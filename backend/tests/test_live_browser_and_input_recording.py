"""
The window you can see, and the input it teaches us.

Two promises the Assisted Apply UI now makes, pinned here:

1. **The window exists while a human step is pending.** A headed pass does not
   close its browser in a ``finally`` the moment it pauses — the window stays
   (registered per session) so the human can sign in, solve the check or press
   Submit in the very context the next pass continues in; a headless pass
   still closes immediately, and a confirmed/terminal session closes too.
   "Start a handoff window" reports honestly whether a window appeared.

2. **What the human types there is recorded for replay — minus secrets.** A
   committed field (never a keystroke stream, never a password/code/PIN/SSN)
   lands as: checkpoint status ``user_completed`` + fingerprint, the session
   working set, and a per-host *learned answers* store that future sessions
   merge into their fill values. The journal records the field name and that
   the human typed it — never the value.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.models.models import Job, Profile, User
from app.services import assisted_fill, live_browser
from app.services import browser_session as sessions
from app.services.assisted_fill import RecordingDriver
from app.services.user_settings import set_setting


@pytest.fixture()
def profile(db):
    row = Profile(user_id=db.query(User).order_by(User.id).first().id,
                  data={"name": "Ada Lovelace", "firstName": "Ada", "lastName": "Lovelace",
                        "email": "ada@example.com"},
                  layout={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _job(db, user_id: int, *, url: str = "https://jobs.lever.co/acme/1234") -> Job:
    # The external id (and with it the dedupe key) is per-URL: one test may
    # open several postings for the same user.
    external_id = url.rstrip("/").rsplit("/", 1)[-1] or "req"
    job = Job(user_id=user_id, title="Senior Backend Engineer", company="Acme", url=url,
              external_id=external_id, dedupe_key=f"url:{url}", status="ready_to_apply")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _page(*fields: dict, url: str = "https://jobs.lever.co/acme/1234",
          markers: list | None = None, **extra) -> dict:
    return {"url": url, "host": "jobs.lever.co", "title": "Apply", "fields": list(fields),
            "markers": markers or [], **extra}


def _field(name: str, label: str, type_: str = "text", **extra) -> dict:
    return {"name": name, "label": label, "type": type_, "required": True, **extra}


class FakeWindow:
    """A headed window stand-in: healthy until closed, can be parked on a URL."""

    def __init__(self, mode: str = "headed") -> None:
        self.closed = False
        self.shown: list[str] = []
        self.opened = False
        self.exported = False
        self.launch_report = {"mode": mode, "mode_reason": "test"}

    def healthy(self) -> bool:
        return not self.closed

    async def open(self, session) -> None:
        self.opened = True

    async def show(self, url: str) -> bool:
        self.shown.append(url)
        return True

    async def export_storage_state(self):
        self.exported = True
        # One real-shaped cookie: an *empty* jar is honestly "nothing to
        # persist", and the test is about the hand-off, not that refusal.
        return {"cookies": [{"name": "portal_session", "value": "abc123",
                             "domain": ".jobs.lever.co", "path": "/", "expires": -1,
                             "httpOnly": True, "secure": True, "sameSite": "Lax"}],
                "origins": []}

    async def close(self) -> None:
        self.closed = True


def _start(db, job):
    user = db.query(User).order_by(User.id).first()
    session, _created = sessions.start_session(db, user=user, job=job)
    return user, session


def _age_entry(session_id: int, seconds: float) -> None:
    entry = live_browser._ENTRIES.get(session_id)
    assert entry is not None
    entry.last_used -= seconds


# --------------------------------------------------------------------------- #
# The registry: a window's lifetime is the session's, not the pass's
# --------------------------------------------------------------------------- #
class TestLiveWindowRegistry:
    def test_a_registered_window_is_adopted_and_closed_on_demand(self):
        async def scenario():
            win = FakeWindow()
            live_browser.remember(7001, win)
            assert live_browser.adopt(7001) is win
            assert live_browser.holds(7001, win)
            assert live_browser.status(7001)["open"] is True
            await live_browser.close(7001)
            assert win.closed is True
            assert live_browser.status(7001)["open"] is False
        asyncio.run(scenario())

    def test_a_window_the_user_closed_is_dropped_not_adopted(self):
        async def scenario():
            win = FakeWindow()
            live_browser.remember(7002, win)
            win.closed = True
            assert live_browser.adopt(7002) is None
            assert live_browser.status(7002)["open"] is False
        asyncio.run(scenario())

    def test_an_idle_window_is_swept_after_the_ttl(self):
        async def scenario():
            win = FakeWindow()
            live_browser.remember(7003, win)
            _age_entry(7003, live_browser.IDLE_TTL_SECONDS + 5)
            assert live_browser.adopt(7003) is None  # sweep drops it
            await asyncio.sleep(0)                   # the close task runs
            assert win.closed is True
        asyncio.run(scenario())

    def test_status_is_a_plain_fact_no_driver_no_value(self):
        payload = live_browser.status(999999)
        assert payload == {"open": False}
        assert live_browser.any_open()["open"] >= 0


# --------------------------------------------------------------------------- #
# run_pass keeps a *headed* window open only while a human step is pending
# --------------------------------------------------------------------------- #
class TestPassWindowLifetime:
    def test_a_paused_headed_pass_keeps_the_window_open(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        driver = RecordingDriver(observations=[_page(markers=["captcha"])])
        driver.launch_report = {"mode": "headed"}
        try:
            result = asyncio.run(assisted_fill.run_pass(db, user=user, session=session,
                                                        driver=driver))
            assert result["pause"] == "captcha"
            assert driver.closed is False
            assert live_browser.adopt(session.id) is driver
        finally:
            asyncio.run(live_browser.close(session.id))
        assert driver.closed is True

    def test_a_headless_pass_still_closes_at_once(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        driver = RecordingDriver(observations=[_page(markers=["captcha"])])
        driver.launch_report = {"mode": "headless"}
        asyncio.run(assisted_fill.run_pass(db, user=user, session=session, driver=driver))
        assert driver.closed is True
        assert live_browser.adopt(session.id) is None

    def test_a_portal_confirmation_closes_the_session_and_the_window(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        driver = RecordingDriver(observations=[_page(confirmation="submitted")])
        driver.launch_report = {"mode": "headed"}
        result = asyncio.run(assisted_fill.run_pass(db, user=user, session=session, driver=driver))
        assert result["confirmed"] == "submitted"
        assert session.state == "completed"
        assert driver.closed is True
        assert live_browser.adopt(session.id) is None


# --------------------------------------------------------------------------- #
# "Start a handoff window" opens the window — or says exactly why it could not
# --------------------------------------------------------------------------- #
class TestHandoffWindow:
    def test_headless_hosts_report_no_window_instead_of_pretending(self, db, owner, profile,
                                                                   monkeypatch):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        monkeypatch.setattr(assisted_fill, "assisted_apply_available",
                            lambda: {"available": True})
        monkeypatch.setattr(assisted_fill, "browser_launch_plan",
                            lambda: {"available": True, "headless": True, "mode": "headless",
                                     "mode_reason": "no display (DISPLAY unset)"})
        answer = asyncio.run(assisted_fill.open_handoff_window(
            db, user=user, session=session, url="https://jobs.lever.co/acme/1234"))
        assert answer["window_opened"] is False
        assert answer["mode"] == "headless"
        assert "display" in answer["reason"]
        assert live_browser.status(session.id)["open"] is False

    def test_a_display_capable_host_gets_a_real_parked_window(self, db, owner, profile,
                                                               monkeypatch):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        win = FakeWindow()
        monkeypatch.setattr(assisted_fill, "assisted_apply_available",
                            lambda: {"available": True})
        monkeypatch.setattr(assisted_fill, "browser_launch_plan",
                            lambda: {"available": True, "headless": False, "mode": "headed",
                                     "mode_reason": "a display is available"})
        monkeypatch.setattr(assisted_fill, "build_driver",
                            lambda *args, **kwargs: win)
        try:
            answer = asyncio.run(assisted_fill.open_handoff_window(
                db, user=user, session=session, url="https://jobs.lever.co/acme/1234"))
            assert answer == {"window_opened": True, "mode": "headed"}
            assert win.shown == ["https://jobs.lever.co/acme/1234"]
            assert live_browser.adopt(session.id) is win
        finally:
            asyncio.run(live_browser.close(session.id))
        assert win.closed is True

    def test_a_headed_launch_that_falls_back_to_headless_is_not_claimed(self, db, owner, profile,
                                                                        monkeypatch):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        fallen = FakeWindow(mode="headless")  # launch_report says it fell back
        monkeypatch.setattr(assisted_fill, "assisted_apply_available",
                            lambda: {"available": True})
        monkeypatch.setattr(assisted_fill, "browser_launch_plan",
                            lambda: {"available": True, "headless": False, "mode": "headed",
                                     "mode_reason": "a display is available"})
        monkeypatch.setattr(assisted_fill, "build_driver", lambda *args, **kwargs: fallen)
        answer = asyncio.run(assisted_fill.open_handoff_window(
            db, user=user, session=session, url="https://jobs.lever.co/acme/1234"))
        assert answer["window_opened"] is False
        assert fallen.closed is True            # no invisible window is kept alive
        assert live_browser.status(session.id)["open"] is False

    def test_the_handoff_route_puts_the_honest_answer_in_the_descriptor(self, client, db, owner,
                                                                        auth, profile, monkeypatch):
        from app.models.models import ApplicationAction

        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id)
        _user, session = _start(db, job)
        sessions.record_observation(db, session,
                                    _page(fields=[_field("first_name", "First name")],
                                          markers=["captcha"]),
                                    user=user)
        action = (db.query(ApplicationAction)
                  .filter(ApplicationAction.session_id == session.id).first())
        assert action is not None

        async def fake_open(_db, *, user, session, url=""):
            return {"window_opened": True, "mode": "headed"}

        monkeypatch.setattr(assisted_fill, "open_handoff_window", fake_open)
        response = client.post(f"/api/application-sessions/{session.id}"
                               f"/actions/{action.id}/handoff", json={}, headers=auth)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["window_opened"] is True
        assert payload["mode"] == "headed"
        assert payload["token"].startswith("ho_")
        # The token itself still never becomes a window claim: it is minted once.
        assert "window_opened" in payload


# --------------------------------------------------------------------------- #
# What the human typed: recorded for replay, minus everything secret
# --------------------------------------------------------------------------- #
class TestBrowserInputRecording:
    def test_a_committed_field_is_recorded_taught_and_journalled(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        entry = {"name": "how_did_you_hear", "label": "How did you hear about us?",
                 "type": "text", "autocomplete": "", "value": "Employee referral"}
        result = sessions.record_browser_input(db, session, entry)
        db.commit()
        assert result == {"recorded": ["how_did_you_hear"], "restricted": []}

        field = dict((session.checkpoint or {}).get("fields", {}).get("how_did_you_hear"))
        assert field["status"] == "user_completed"
        assert field["source"] == "user_in_browser"
        assert field["value_fingerprint"] == sessions.fingerprint_value("Employee referral")
        assert "value" not in field                      # the checkpoint never holds it

        assert sessions.session_values(session)["how_did_you_hear"] == "Employee referral"
        host = sessions.learning_host(session)
        learned = sessions.learned_values(db, user_id=user.id, host=host)
        assert learned["how_did_you_hear"] == "Employee referral"

        steps = (session.checkpoint or {}).get("steps") or []
        last = steps[-1]
        assert last["event"] == "fill"
        assert last["reason"] == "user_in_browser"
        assert last["fields"] == ["how_did_you_hear"]
        # The value appears nowhere in the checkpoint payload.
        assert "Employee referral" not in json.dumps(session.checkpoint)

    @pytest.mark.parametrize("entry", [
        {"name": "password", "label": "Password", "type": "password", "value": "hunter2"},
        {"name": "otp", "label": "Enter code", "type": "text",
         "autocomplete": "one-time-code", "value": "123456"},
        {"name": "ssn", "label": "Social Security Number", "type": "text", "value": "000-00-0000"},
        {"name": "cvv", "label": "Card security code", "type": "text", "value": "999"},
    ])
    def test_secrets_are_refused_and_stored_nowhere(self, db, owner, profile, entry):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        result = sessions.record_browser_input(db, session, entry)
        db.commit()
        assert result["recorded"] == []
        assert result["restricted"] == [entry["name"]]
        assert sessions.session_values(session) == {}
        assert sessions.learned_values(db, user_id=user.id,
                                       host=sessions.learning_host(session)) == {}
        assert (session.checkpoint or {}).get("fields") in ({}, None)
        blob = json.dumps({"checkpoint": session.checkpoint, "fill": session.fill_values})
        assert entry["value"] not in blob

    def test_the_classifier_can_veto_a_field_the_patterns_miss(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        # "Government ID" is restricted by *wording*, not by the name/type
        # filters above: the classifier is the last, strictest gate.
        result = sessions.record_browser_input(db, session, {
            "name": "gov_id_17", "label": "Government ID",
            "type": "text", "value": "L8920344"})
        db.commit()
        assert result["recorded"] == []
        assert result["restricted"] == ["gov_id_17"]
        assert sessions.learned_values(db, user_id=user.id,
                                       host=sessions.learning_host(session)) == {}

    def test_the_recorded_value_reaches_the_next_pass_and_learned_ones_a_future_session(
            self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id)
        _user, session = _start(db, job)
        sessions.record_browser_input(db, session, {
            "name": "referral_code", "label": "Referral code", "type": "text", "value": "XJ-9"})
        db.commit()

        # This session: the answer joins the fill values …
        values = assisted_fill.build_instruction_values(job, session,
                                                        answers=sessions.session_values(session),
                                                        db=db)
        assert values["referral_code"] == "XJ-9"
        # … and a *future* session on the same host learns it without any
        # carry-over from this session's working set (which ends with it).
        future_job = _job(db, user.id, url="https://jobs.lever.co/acme/5678")
        _u, future = _start(db, future_job)
        assert sessions.session_values(future) == {}
        values = assisted_fill.build_instruction_values(future_job, future, answers={}, db=db)
        assert values["referral_code"] == "XJ-9"
        # Live-session slots belong to live sessions: close these two before
        # opening the third (per-user window budget).
        sessions.cancel_session(db, user=user, session=session)
        sessions.cancel_session(db, user=user, session=future)
        db.commit()
        # … while a session against another host does not borrow it.
        other_job = _job(db, user.id, url="https://boards.greenhouse.io/acme/9")
        _u, other = _start(db, other_job)
        values = assisted_fill.build_instruction_values(other_job, other, answers={}, db=db)
        assert "referral_code" not in values

    def test_a_session_answer_outranks_a_learned_one(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id)
        _user, session = _start(db, job)
        sessions.learn_answer(db, user_id=user.id, host="jobs.lever.co",
                              name="referral_code", value="OLD")
        sessions.remember_values(session, {"referral_code": "NEW"})
        db.commit()
        values = assisted_fill.build_instruction_values(job, session,
                                                        answers=sessions.session_values(session),
                                                        db=db)
        assert values["referral_code"] == "NEW"

    def test_in_app_answers_are_learned_for_future_sessions_too(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id)
        _user, session = _start(db, job)
        sessions.record_user_answers(db, session, {"notice_period": "30 days"})
        db.commit()
        learned = sessions.learned_values(db, user_id=user.id, host=sessions.learning_host(session))
        assert learned["notice_period"] == "30 days"

    def test_the_learned_store_is_bounded(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        for index in range(sessions.LEARNED_ANSWERS_MAX + 25):
            sessions.learn_answer(db, user_id=user.id, host="jobs.lever.co",
                                  name=f"q_{index}", value=str(index), commit=False)
        db.commit()
        raw = sessions.learned_values(db, user_id=user.id, host="jobs.lever.co")
        assert len(raw) == sessions.LEARNED_ANSWERS_MAX


# --------------------------------------------------------------------------- #
# Closing the loop: the window's state is on the payload, and completing a
# handoff hands the cookie jar to the next pass
# --------------------------------------------------------------------------- #
class TestWindowOnSessionPayload:
    def test_the_session_payload_says_whether_the_window_is_open(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        _user, session = _start(db, job)
        assert sessions.session_detail(db, session=session)["window"] == {"open": False}

        async def scenario():
            live_browser.remember(session.id, FakeWindow())
        asyncio.run(scenario())
        try:
            detail = sessions.session_detail(db, session=session)
            assert detail["window"]["open"] is True
            # No driver, no loop, no URL leaks into the API payload.
            assert set(detail["window"]) <= {"open", "idle_seconds", "age_seconds"}
        finally:
            asyncio.run(live_browser.close(session.id))

    def test_completing_a_handoff_exports_the_window_state_when_persistence_is_on(
            self, client, db, owner, auth, profile, monkeypatch):
        from app.models.models import ApplicationAction

        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id)
        _u, session = _start(db, job)
        sessions.record_observation(db, session,
                                    _page(fields=[_field("first_name", "First name")],
                                          markers=["captcha"]),
                                    user=user)
        action = (db.query(ApplicationAction)
                  .filter(ApplicationAction.session_id == session.id).first())
        assert action is not None and session.state == "awaiting_user"

        set_setting(db, user.id, "browser", "persist_session", True)
        db.commit()
        win = FakeWindow()
        monkeypatch.setattr(live_browser, "adopt", lambda sid: win if sid == session.id else None)

        response = client.post(f"/api/application-sessions/{session.id}"
                               f"/actions/{action.id}/complete",
                               json={"note": "done in the browser"}, headers=auth)
        assert response.status_code == 200, response.text
        assert win.exported is True
        db.refresh(session)
        assert session.state == "paused"
        detail = sessions.session_detail(db, session=session)
        assert detail["persistence"]["enabled"] is True
