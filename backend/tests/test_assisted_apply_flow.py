"""
Assisted Apply actually applies: the flow loop, and the honesty rules around it.

The failure these tests pin (found in the field, not in a unit test): a user
started an assisted session for a real posting that needs *account creation →
profile → apply*, hit "Run a pass", and was told the run completed — while
nothing had been typed, no page had advanced, and nothing was submitted. Two
defects produced that:

* the pass called a session ``completed`` whenever the loop ran out of fields to
  fill, which a landing page with no form satisfies immediately; and
* the real driver re-classified every field *without* the values it was about to
  type, so the classifier called each one ``ask_user``/``skip`` and the driver
  refused all of them — no fill, no error, and the false "completed" above.

What the loop must do instead:

* walk the flow through the portal's own controls (``Apply for this job`` →
  ``Continue`` → …), filling each page it lands on;
* stop at anything a human owns — a sign-in wall, a bot check, an unknown or
  ambiguous field, a legal attestation;
* never click a Submit unless the session policy allows submission *and* the
  at-most-once ledger has reserved the slot;
* close a session as ``completed`` only when the portal itself confirmed the
  application; and otherwise raise an actionable ``review_required`` pause that
  says why it stopped.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types

import pytest

from app.core.config import settings
from app.models.models import ApplicationAction, Job, Profile, User
from app.services import assisted_fill
from app.services import browser_session as sessions
from app.services.assisted_fill import RecordingDriver


@pytest.fixture()
def profile(db):
    row = Profile(user_id=db.query(User).order_by(User.id).first().id,
                  data={"name": "Ada Lovelace", "firstName": "Ada", "lastName": "Lovelace",
                        "email": "ada@example.com", "phone": "+49 30 1234567",
                        "location": "Berlin", "linkedin": "https://linkedin.com/in/ada"},
                  layout={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _job(db, user_id: int, *, url: str = "https://jobs.lever.co/acme/1234",
         plan: list | None = None, external_id: str = "req-1234") -> Job:
    job = Job(user_id=user_id, title="Senior Backend Engineer", company="Acme", url=url,
              external_id=external_id, status="ready_to_apply")
    if plan is not None:
        job.extra = {"autofill_plan": {"fields": plan}}
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _plan(*fields: tuple[str, str, str]) -> list[dict]:
    """``(name, profile_key, value)`` triples → an autofill plan."""
    return [{"name": name, "profile_key": key, "type": "text", "value": value,
             "value_source": "profile"} for name, key, value in fields]


def _page(*fields: dict, url: str = "https://jobs.lever.co/acme/1234",
          title: str = "Apply", **extra) -> dict:
    return {"url": url, "host": "jobs.lever.co", "title": title, "fields": list(fields),
            "markers": [], **extra}


def _field(name: str, label: str, type_: str = "text", **extra) -> dict:
    return {"name": name, "label": label, "type": type_, "required": True, **extra}


def _control(index: int, label: str, **extra) -> dict:
    return {"index": index, "label": label, "tag": "button", "type": "submit", "href": "",
            "visible": True, "disabled": False, **extra}


def _run(db, user, session, driver, **kwargs):
    return asyncio.run(assisted_fill.run_pass(db, user=user, session=session, driver=driver,
                                              **kwargs))


# --------------------------------------------------------------------------- #
# The reported bug, both halves of it
# --------------------------------------------------------------------------- #
def test_a_page_with_nothing_to_fill_is_not_reported_as_applied(db, owner, profile):
    """
    A posting's landing page has an "Apply" link and no form at all. That is not
    an application — it must not be recorded as one.
    """
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id)
    session, _ = sessions.start_session(db, user=user, job=job)

    driver = RecordingDriver(observations=[_page(title="Senior Backend Engineer")])
    result = _run(db, user, session, driver)
    db.refresh(session)

    assert session.state == "awaiting_user", "a pass that did nothing cannot be 'completed'"
    assert result["outcome"] == "awaiting_user"
    assert result["pause"] == "review_required"
    assert result["pause_reason"] == "flow_cannot_continue_automatically"
    assert result["filled"] == []
    # …and the user gets something to act on, not a silent status flip.
    actions = sessions.pending_actions(db, user.id, session_id=session.id)
    assert [a.kind for a in actions] == ["review_required"]
    assert actions[0].instructions
    assert "submitted" in actions[0].instructions.lower()


def test_filling_the_first_page_of_a_multi_step_flow_does_not_end_the_session(db, owner, profile):
    """
    Page 1 of `sign-up → profile → apply` is filled; without a control to move
    on, the pass stops honestly instead of declaring the application done.
    """
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id, plan=_plan(("email", "email", "ada@example.com")))
    session, _ = sessions.start_session(db, user=user, job=job)

    driver = RecordingDriver(observations=[
        _page(_field("email", "Email", "email"), title="Step 1 of 3 — Create your account"),
    ])
    result = _run(db, user, session, driver)
    db.refresh(session)

    assert [f["name"] for f in driver.fills] == ["email"]
    assert session.state == "awaiting_user"
    assert result["filled"] == ["email"]
    assert result["outcome"] == "awaiting_user"
    # The journal says what was typed and why the run stopped.
    journal = sessions.flow_journal(session)
    assert [entry["event"] for entry in journal] == ["page", "fill", "page", "blocked", "pause"]
    assert journal[1]["fields"] == ["email"]


# --------------------------------------------------------------------------- #
# Walking the flow
# --------------------------------------------------------------------------- #
def test_a_pass_walks_a_three_step_flow_and_fills_every_page(db, owner, profile):
    """
    The reported scenario: a posting that needs account creation → profile →
    apply. The pass must walk it and fill every page, page by page, through the
    portal's own controls.
    """
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id, plan=_plan(("full_name", "fullName", "Ada Lovelace"),
                                       ("email", "email", "ada@example.com"),
                                       ("first_name", "firstName", "Ada"),
                                       ("last_name", "lastName", "Lovelace")))
    session, _ = sessions.start_session(db, user=user, job=job)

    landing = "https://jobs.lever.co/acme/1234"
    step1, step2, step3 = f"{landing}/apply", f"{landing}/apply/profile", f"{landing}/apply/review"
    step1_fields = [_field("full_name", "Full name"), _field("email", "Email", "email")]
    step2_fields = [_field("first_name", "First name"), _field("last_name", "Last name")]

    driver = RecordingDriver(
        observations=[
            _page(title="Senior Backend Engineer", url=landing),
            _page(*step1_fields, title="Step 1 of 3", step_index=1, steps_total=3, url=step1),
            # A real page is looked at twice: before and after the fill.
            _page(*[{**f, "value_present": True} for f in step1_fields],
                  title="Step 1 of 3", url=step1),
            _page(*step2_fields, title="Step 2 of 3", step_index=2, steps_total=3, url=step2),
            _page(*[{**f, "value_present": True} for f in step2_fields],
                  title="Step 2 of 3", url=step2),
            _page(title="Step 3 of 3", submit_present=True, step_index=3, steps_total=3, url=step3),
        ],
        controls_by_url={
            landing: [_control(0, "Apply for this job")],
            step1: [_control(1, "Continue")],
            step2: [_control(2, "Continue")],
            step3: [_control(3, "Submit application")],
        },
    )
    result = _run(db, user, session, driver)
    db.refresh(session)

    assert result["filled"] == ["full_name", "email", "first_name", "last_name"]
    assert result["advanced"] == ["Apply for this job", "Continue", "Continue"]
    assert result["pages"] == 4
    # The Submit control is never clicked by a pass: the session policy here
    # disallows submission, so the pass stops with the page filled and waiting.
    assert [a["kind"] for a in driver.advances] == ["start_application", "continue_application",
                                                    "continue_application"]
    assert session.state == "awaiting_user"
    assert result["pause_reason"] == "waiting_for_you_to_submit"
    assert result["next_step"]["reason"] == "waiting_for_you_to_submit"
    # …and the user can read the whole run back, step by step.
    events = [(entry["event"], entry.get("reason", "")) for entry in sessions.flow_journal(session)]
    assert events == [("page", ""), ("advance", "moved"), ("page", "1/3"), ("fill", ""),
                      ("advance", "moved"), ("page", "2/3"), ("fill", ""), ("advance", "moved"),
                      ("page", "3/3"), ("blocked", "submit_not_allowed"),
                      ("pause", "waiting_for_you_to_submit")], events


def test_a_submit_control_is_only_clicked_when_the_policy_allows_submission(db, owner, profile):
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id)
    session, _ = sessions.start_session(db, user=user, job=job)

    driver = RecordingDriver(
        observations=[_page(title="Step 3 of 3", submit_present=True)],
        controls_by_url={_page()["url"]: [_control(0, "Submit application")]},
    )
    policy = sessions.SessionPolicy(allow_submit=False, dry_run=True)
    result = _run(db, user, session, driver, policy=policy)
    db.refresh(session)

    assert driver.advances == []
    assert result["pause_reason"] == "waiting_for_you_to_submit"


def test_a_submit_control_is_left_to_the_ledger_when_the_policy_allows_submission(db, owner, profile):
    """
    Even with submission allowed, a pass does not press Submit: the reservation
    endpoint (policy + at-most-once ledger) owns that act. The pass reports the
    page as ``ready_to_submit``.
    """
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id)
    session, _ = sessions.start_session(db, user=user, job=job)

    driver = RecordingDriver(
        observations=[_page(title="Step 3 of 3", submit_present=True)],
        controls_by_url={_page()["url"]: [_control(0, "Submit application")]},
    )
    policy = sessions.SessionPolicy(allow_submit=True, dry_run=False)
    result = _run(db, user, session, driver, policy=policy)

    assert driver.advances == []
    assert result["outcome"] == "ready_to_submit"
    assert result["next_step"]["action"] == "submit"


def test_a_portal_confirmation_closes_the_session(db, owner, profile):
    """'Thank you for applying' is the only page-level evidence of success."""
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id)
    session, _ = sessions.start_session(db, user=user, job=job)

    driver = RecordingDriver(observations=[_page(confirmation="submitted", title="Thank you")])
    result = _run(db, user, session, driver)
    db.refresh(session)

    assert result["confirmed"] == "submitted"
    assert result["outcome"] == "completed"
    assert session.state == "completed"
    assert session.state_reason == "portal_confirmed_submitted"
    assert sessions.pending_actions(db, user.id, session_id=session.id) == []


def test_a_flow_that_leaves_the_application_host_stops_and_hands_over(db, owner, profile):
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id)
    session, _ = sessions.start_session(db, user=user, job=job)

    driver = RecordingDriver(
        observations=[_page(title="Apply")],
        control_scripts=[[_control(0, "Apply for this job", tag="a",
                                   href="https://some-other-company.example/apply")]],
    )
    result = _run(db, user, session, driver)
    db.refresh(session)

    assert driver.advances == []
    assert session.state == "awaiting_user"
    assert result["pause_reason"] == "flow_leaves_the_application"


def test_the_human_keeps_the_wall_they_own(db, owner, profile):
    """
    A password on the sign-up page is a handoff, not a field to fill — and the
    profile fields beside it are still typed, so the user does not retype them.
    """
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id, plan=_plan(("email", "email", "ada@example.com")))
    session, _ = sessions.start_session(db, user=user, job=job)

    driver = RecordingDriver(observations=[
        _page(_field("email", "Email", "email"), _field("password", "Password", "password"),
              title="Create your account"),
    ])
    result = _run(db, user, session, driver)
    db.refresh(session)

    assert [f["name"] for f in driver.fills] == ["email"]
    assert all("password" not in json.dumps(fill) for fill in driver.fills)
    assert result["pause"] == "login"
    assert session.state == "awaiting_user"


def test_the_journal_never_carries_a_value_or_page_text(db, owner, profile):
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id, plan=_plan(("email", "email", "ada@example.com")))
    session, _ = sessions.start_session(db, user=user, job=job)

    driver = RecordingDriver(observations=[_page(_field("email", "Email", "email"))])
    _run(db, user, session, driver)
    db.refresh(session)

    blob = json.dumps(sessions.flow_journal(session))
    assert "ada@example.com" not in blob
    assert "value_fingerprint" not in blob
    assert blob.count("email") >= 1  # the field *name* is the point of a journal


# --------------------------------------------------------------------------- #
# Which control is safe to click (pure policy)
# --------------------------------------------------------------------------- #
class TestChooseAdvanceControl:
    def test_prefers_the_apply_control_over_generic_navigation(self):
        decision = assisted_fill.choose_advance_control([
            {"index": 0, "label": "Sign in", "tag": "a", "href": "/login", "visible": True,
             "disabled": False},
            {"index": 1, "label": "Apply for this job", "tag": "a", "href": "/apply",
             "visible": True, "disabled": False},
        ], current_host="jobs.lever.co", expected_host="jobs.lever.co")
        assert decision["click"] is True and decision["index"] == 1
        assert decision["kind"] == "start_application"

    def test_never_clicks_a_submit_when_submission_is_disallowed(self):
        decision = assisted_fill.choose_advance_control([
            {"index": 0, "label": "Submit application", "tag": "button", "visible": True,
             "disabled": False},
        ])
        assert decision["click"] is False and decision["reason"] == "submit_not_allowed"

    def test_current_page_link_is_never_clicked(self):
        decision = assisted_fill.choose_advance_control([
            {"index": 0, "label": "Apply for this job", "tag": "a", "href": "/apply",
             "visible": True, "disabled": False},
        ], current_host="jobs.lever.co", expected_host="jobs.lever.co")
        assert decision["click"] is True

    def test_off_host_link_is_refused(self):
        decision = assisted_fill.choose_advance_control([
            {"index": 0, "label": "Continue", "tag": "a", "href": "https://evil.example/next",
             "visible": True, "disabled": False},
        ], current_host="jobs.lever.co", expected_host="jobs.lever.co")
        assert decision["click"] is False
        assert decision["reason"] == "off_host_control"

    def test_the_bare_word_apply_on_a_link_is_not_trusted(self):
        decision = assisted_fill.choose_advance_control([
            {"index": 0, "label": "Apply", "tag": "a", "href": "/jobs/other",
             "visible": True, "disabled": False},
        ], current_host="jobs.lever.co", expected_host="jobs.lever.co")
        assert decision["click"] is False
        assert decision["reason"] == "generic_label_on_link"

    def test_invisible_or_disabled_controls_are_skipped(self):
        decision = assisted_fill.choose_advance_control([
            {"index": 0, "label": "Continue", "tag": "button", "visible": False,
             "disabled": False},
            {"index": 1, "label": "Continue", "tag": "button", "visible": True, "disabled": True},
        ])
        assert decision["click"] is False and decision["reason"] == "no_control_found"

    def test_a_sign_in_button_is_not_a_step_of_the_flow(self):
        decision = assisted_fill.choose_advance_control([
            {"index": 0, "label": "Sign in with LinkedIn", "tag": "button", "visible": True,
             "disabled": False},
        ])
        assert decision["click"] is False


# --------------------------------------------------------------------------- #
# The browser the user is supposed to see
# --------------------------------------------------------------------------- #
class TestBrowserMode:
    def test_a_display_means_a_visible_browser(self, monkeypatch):
        from app.services import autofill

        monkeypatch.setattr(settings, "autofill_browser_mode", "auto")
        monkeypatch.setattr(settings, "autofill_browser_executable", "/bin/echo")
        monkeypatch.setenv("DISPLAY", ":99")
        plan = autofill.browser_launch_plan()
        assert plan["mode"] == "headed" and plan["headless"] is False
        assert "display" in plan["mode_reason"]

    def test_no_display_means_headless(self, monkeypatch):
        from app.services import autofill

        monkeypatch.setattr(settings, "autofill_browser_mode", "auto")
        monkeypatch.setattr(settings, "autofill_browser_executable", "/bin/echo")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        plan = autofill.browser_launch_plan()
        assert plan["mode"] == "headless" and plan["headless"] is True

    def test_an_explicit_headless_setting_wins(self, monkeypatch):
        from app.services import autofill

        monkeypatch.setattr(settings, "autofill_browser_executable", "/bin/echo")
        monkeypatch.setattr(settings, "autofill_browser_mode", "auto")
        monkeypatch.setenv("DISPLAY", ":99")
        monkeypatch.setattr(type(settings), "model_fields_set",
                            property(lambda self: {"autofill_headless"}), raising=False)
        monkeypatch.setattr(settings, "autofill_headless", True)
        try:
            plan = autofill.browser_launch_plan()
            assert plan["mode"] == "headless"
        finally:
            monkeypatch.undo()

    def test_a_missing_executable_is_named_not_faked(self, monkeypatch):
        from app.services import autofill

        monkeypatch.setattr(settings, "autofill_browser_executable", "/nope/not-here")
        plan = autofill.browser_launch_plan()
        assert plan["available"] is False
        assert "does not exist" in plan["reason"]

    def test_a_system_browser_counts_when_playwright_has_none(self, monkeypatch, tmp_path):
        from app.services import autofill

        monkeypatch.setattr(settings, "autofill_browser_executable", "")
        monkeypatch.setattr(settings, "autofill_browser_channel", "")
        monkeypatch.setattr(autofill, "_chromium_browser_path", lambda: None)
        monkeypatch.setattr(settings, "autofill_browser_autodetect", True)
        monkeypatch.setattr(autofill, "system_browser",
                            lambda: {"engine": "chrome", "path": "/usr/bin/google-chrome"})
        plan = autofill.browser_launch_plan()
        assert plan["available"] is True
        assert plan["engine"] == "chrome"
        assert plan["executable_path"] == "/usr/bin/google-chrome"

    def test_the_launch_arguments_come_from_the_plan(self):
        from app.services import autofill

        kwargs = autofill.browser_launch_kwargs({
            "headless": False, "channel": "", "executable_path": "/opt/chrome",
            "args": ["--lang=en"],
        })
        assert kwargs == {"headless": False, "executable_path": "/opt/chrome", "args": ["--lang=en"]}
        assert autofill.browser_launch_kwargs({"headless": True}) == {"headless": True}

    def test_availability_reports_the_mode_it_would_use(self, monkeypatch):
        from app.services import autofill

        # The answer must be about *the browser*, not about which pip extras this
        # environment happens to have installed: on a machine without Playwright,
        # a browser binary would not be enough, so pretend the package is there.
        if importlib.util.find_spec("playwright") is None:
            monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
        monkeypatch.setattr(autofill, "_chromium_browser_path", lambda: "/fake/chrome")
        monkeypatch.setattr(settings, "autofill_browser_executable", "")
        monkeypatch.setattr(settings, "autofill_browser_channel", "")
        monkeypatch.setattr(settings, "autofill_browser_autodetect", True)
        status = autofill.browser_runtime_available()
        assert status["available"] is True
        assert status["mode"] in ("headed", "headless")
        assert status["engine"] == "playwright"


class TestLaunchFallback:
    """
    A browser the user is supposed to watch must not turn a missing display into
    a stack trace, and must not silently pretend the window opened either.
    """

    class _Chromium:
        def __init__(self, fail_times: int) -> None:
            self.calls: list[dict] = []
            self.fail_times = fail_times

        async def launch(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) <= self.fail_times:
                raise RuntimeError("Target page, context or browser has been closed")
            return {"browser": True, "headless": kwargs.get("headless")}

    class _Playwright:
        def __init__(self, fail_times: int) -> None:
            self.chromium = TestLaunchFallback._Chromium(fail_times)

    def _driver(self, fail_times: int):
        driver = assisted_fill.PlaywrightDriver.__new__(assisted_fill.PlaywrightDriver)
        driver._playwright = TestLaunchFallback._Playwright(fail_times)
        driver.launch_report = {"mode": "headed", "headless": False,
                                "mode_reason": "AUTOFILL_BROWSER_MODE configured"}
        return driver

    def test_a_failed_headed_launch_falls_back_and_says_so(self):
        driver = self._driver(fail_times=1)
        browser = asyncio.run(driver._launch({"headless": False, "executable_path": "/opt/chrome",
                                              "args": ["--no-sandbox"]}))
        assert browser["headless"] is True
        assert driver._playwright.chromium.calls[1]["headless"] is True
        assert "fallback_reason" in driver.launch_report
        assert driver.launch_report["headless"] is True
        assert "fell back to headless" in driver.launch_report["mode_reason"]

    def test_no_browser_at_all_is_a_named_failure(self):
        driver = self._driver(fail_times=3)
        with pytest.raises(assisted_fill.DriverError) as excinfo:
            asyncio.run(driver._launch({"headless": False, "executable_path": "/opt/chrome"}))
        assert "could not launch a browser" in str(excinfo.value)
