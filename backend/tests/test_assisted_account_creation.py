"""
Account creation for Assisted Apply — passwords, gated by an explicit opt-in.

The product promise pinned here, layer by layer:

1. **Off by default, twice.** `browser.create_accounts` is a per-user Settings
   opt-in (default false) *and* the deployment carries a ceiling flag — either
   off means the old behaviour exactly: a password field pauses for a login
   handoff and nothing is typed.
2. **On, the pass creates the portal account credential itself.** The first
   sign-up page it sees mints (or reuses) a vault entry for that domain,
   injects the password into the in-memory values for this fill only, and the
   classifier / fill planner / driver each re-prove the opt-in before a
   character is typed (`allow_credentials` at every layer).
3. **The password never escapes the fill.** Not into `session.fill_values`,
   not into the checkpoint (fingerprint only), not into the flow journal (field
   name only), not into logs. MFA and CAPTCHA pauses are never lifted by the
   opt-in.
4. **The credential lands in the vault**, so the existing Chrome/Apple CSV
   export already carries it — no new export surface.
5. **AI accuracy is a second, separate opt-in** (`browser.ai_assist`): it
   flips the previously hardcoded `use_ai=False` in `prepare_form_plan`.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import settings
from app.models.models import Job, Profile, User
from app.services import assisted_fill, field_classifier, user_settings
from app.services import browser_session as sessions
from app.services.assisted_fill import RecordingDriver
from app.services.user_settings import set_setting
from app.services.vault import export_chrome_csv, get_vault_entry_for_domain, list_vault_entries, reveal_password


@pytest.fixture()
def profile(db):
    # ``owner`` creates the user; the profile gives the vault a real email for
    # the generated username (exactly what a sign-up form will be typed).
    row = Profile(user_id=db.query(User).order_by(User.id).first().id,
                  data={"name": "Ada Lovelace", "email": "ada@example.com"},
                  layout={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _job(db, user_id: int, *, url: str = "https://jobs.lever.co/acme/1234") -> Job:
    external_id = url.rstrip("/").rsplit("/", 1)[-1] or "req"
    job = Job(user_id=user_id, title="Senior Backend Engineer", company="Acme", url=url,
              external_id=external_id, dedupe_key=f"url:{url}", status="ready_to_apply")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _signup_page(*, url: str = "https://jobs.lever.co/acme/1234") -> dict:
    """A sign-up step: password + confirm, both typed only when opted in."""
    return {
        "url": url, "host": "jobs.lever.co", "title": "Create your account",
        "markers": ["sign up"],
        "fields": [
            {"name": "password", "label": "Password", "type": "password", "required": True},
            {"name": "confirm_password", "label": "Confirm password", "type": "password",
             "required": True, "autocomplete": "new-password"},
        ],
    }


def _start(db, job):
    user = db.query(User).order_by(User.id).first()
    session, _created = sessions.start_session(db, user=user, job=job)
    return user, session


def _policy(**kwargs) -> sessions.SessionPolicy:
    return sessions.SessionPolicy(**kwargs)


# --------------------------------------------------------------------------- #
# 1. The opt-in itself: default off, setting on, ceiling wins
# --------------------------------------------------------------------------- #
class TestCreateAccountsPolicy:
    def test_off_by_default(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        policy = sessions.effective_policy(db, user.id)
        assert policy.create_accounts is False
        assert policy.as_dict()["create_accounts"] is False

    def test_user_opt_in_turns_it_on(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        set_setting(db, user.id, "browser", "create_accounts", True)
        db.commit()
        assert sessions.effective_policy(db, user.id).create_accounts is True

    def test_server_ceiling_overrides_the_user(self, db, owner, profile, monkeypatch):
        user = db.query(User).order_by(User.id).first()
        set_setting(db, user.id, "browser", "create_accounts", True)
        db.commit()
        monkeypatch.setattr(settings, "browser_create_accounts_enabled", False)
        assert sessions.effective_policy(db, user.id).create_accounts is False

    def test_settings_document_ships_both_toggles_off_and_writable(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        defaults = user_settings.defaults()
        assert defaults["browser"]["create_accounts"] is False
        assert defaults["browser"]["ai_assist"] is False
        assert {"create_accounts", "ai_assist"} <= user_settings.WRITABLE_KEYS["browser"]
        grouped = user_settings.grouped(db, user)
        assert grouped["browser"]["create_accounts_available"] is settings.browser_create_accounts_enabled
        assert grouped["browser"]["ai_assist_available"] is settings.browser_ai_assist_enabled

    def test_boolean_writes_are_strict_not_truthy(self, db, owner, profile):
        from fastapi import HTTPException

        user = db.query(User).order_by(User.id).first()
        # The *string* "false" must never reach the row as-is: read back it
        # would be truthy and gate password typing on. It is coerced to a real
        # boolean instead…
        result = user_settings.apply_updates(db, user, {"browser": {"create_accounts": "false"}})
        assert result["updated"]["browser"]["create_accounts"] is False
        assert sessions.effective_policy(db, user.id).create_accounts is False
        # …word forms coerce to a real boolean too…
        result = user_settings.apply_updates(db, user, {"browser": {"create_accounts": "yes"}})
        assert result["updated"]["browser"]["create_accounts"] is True
        assert sessions.effective_policy(db, user.id).create_accounts is True
        # …and anything that is not boolean-ish at all is refused outright.
        with pytest.raises(HTTPException) as exc:
            user_settings.apply_updates(db, user, {"browser": {"create_accounts": "maybe"}})
        assert exc.value.status_code == 400

    def test_a_truthy_string_already_in_the_row_does_not_open_the_gate(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        set_setting(db, user.id, "browser", "create_accounts", "yes")
        db.commit()
        assert sessions.effective_policy(db, user.id).create_accounts is False, \
            "the gate is fail-closed: only a literal boolean opt-in counts"


# --------------------------------------------------------------------------- #
# 2. Layer one — the classifier
# --------------------------------------------------------------------------- #
class TestClassifierCredentialVerdicts:
    def test_default_refuses_the_password_even_with_a_value(self):
        verdict = field_classifier.classify_field(
            {"name": "password", "label": "Password", "type": "password"}, value="s3cret")
        assert verdict.classification == "credential"
        assert verdict.action == "handoff"
        assert verdict.pauses is True
        assert verdict.pause_kind == "login"

    def test_allowed_and_valued_is_autofill(self):
        verdict = field_classifier.classify_field(
            {"name": "password", "label": "Password", "type": "password"}, value="s3cret",
            allow_credentials=True)
        assert verdict.classification == "credential"
        assert verdict.action == "autofill"
        assert verdict.pauses is False
        assert verdict.blocks is False

    def test_allowed_without_a_value_still_hands_off(self):
        verdict = field_classifier.classify_field(
            {"name": "password", "label": "Password", "type": "password"},
            allow_credentials=True)
        assert verdict.action == "handoff", "opt-in alone must not clear an empty field"

    def test_allowed_but_already_filled_is_never_typed_again(self):
        verdict = field_classifier.classify_field(
            {"name": "password", "label": "Password", "type": "password"}, value="s3cret",
            checkpoint_status="filled", allow_credentials=True)
        assert verdict.action == "skip"
        assert verdict.pauses is False

    def test_the_allow_flag_unlocks_nothing_else(self):
        # A code and a bot check keep their verdicts even when credentials fly.
        code = field_classifier.classify_field(
            {"name": "otp", "label": "Verification code", "type": "text"}, value="123456",
            allow_credentials=True)
        assert code.classification == "mfa" and code.action == "handoff"
        captcha = field_classifier.classify_field(
            {"name": "g-recaptcha", "label": "Captcha", "type": "text"}, value="tok",
            allow_credentials=True)
        assert captcha.classification == "captcha" and captcha.action == "handoff"

    def test_form_passthrough(self):
        fields = [{"name": "password", "label": "Password", "type": "password"}]
        refused = field_classifier.classify_form(fields, values={"password": "x"})
        assert refused.must_pause and refused.pause_kind == "login"
        allowed = field_classifier.classify_form(fields, values={"password": "x"},
                                                 allow_credentials=True)
        assert allowed.must_pause is False
        assert [v.name for v in allowed.autofillable] == ["password"]


# --------------------------------------------------------------------------- #
# 3. Layer two — the observation planner: pause lifted, value not persisted
# --------------------------------------------------------------------------- #
class TestObservationPlumbing:
    def _session(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        user, session = _start(db, job)
        return job, user, session

    def test_login_pause_holds_by_default_and_lifts_when_allowed_and_valued(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        # Default: the very same page pauses for a login handoff.
        _, session = _start(db, _job(db, user.id, url="https://jobs.lever.co/acme/1001"))
        outcome = sessions.record_observation(db, session, _signup_page(
            url="https://jobs.lever.co/acme/1001"), user=user)
        assert outcome["pause"] == "login"

        # Opted-in session, password in hand: no pause, the fields are autofill.
        _, session2 = _start(db, _job(db, user.id, url="https://jobs.lever.co/acme/1002"))
        outcome = sessions.record_observation(
            db, session2, _signup_page(url="https://jobs.lever.co/acme/1002"), user=user,
            values={"password": "vault-pass", "confirm_password": "vault-pass"},
            allow_credentials=True)
        assert outcome.get("pause") is None
        assert session2.progress["autofillable"] == 2

    def test_the_password_never_reaches_the_working_set(self, db, owner, profile):
        _, user, session = self._session(db, owner, profile)
        sessions.record_observation(
            db, session, _signup_page(), user=user,
            values={"password": "vault-pass", "confirm_password": "vault-pass"},
            allow_credentials=True)
        db.refresh(session)
        assert "vault-pass" not in json.dumps(session.fill_values or {})
        assert list(session.fill_values or {}) == []

    def test_mfa_and_captcha_still_pause_when_allowed(self, db, owner, profile):
        _, user, session = self._session(db, owner, profile)
        mfa_page = _signup_page()
        mfa_page["markers"] = ["verification code"]
        mfa_page["fields"] = [{"name": "otp", "label": "Verification code", "type": "text"}]
        outcome = sessions.record_observation(db, session, mfa_page, user=user,
                                               allow_credentials=True)
        assert outcome["pause"] == "mfa"

        user, session2 = _start(db, _job(db, user.id, url="https://jobs.lever.co/acme/7777"))
        captcha_page = _signup_page(url="https://jobs.lever.co/acme/7777")
        captcha_page["markers"] = ["recaptcha"]
        outcome = sessions.record_observation(db, session2, captcha_page, user=user,
                                               allow_credentials=True)
        assert outcome["pause"] == "captcha"

    def test_fill_instructions_carry_the_password_only_when_allowed(self, db, owner, profile):
        _, user, session = self._session(db, owner, profile)
        observation = _signup_page()
        values = {"password": "vault-pass", "confirm_password": "vault-pass"}
        sessions.record_observation(db, session, observation, user=user,
                                    values=values, allow_credentials=True)
        allowed = sessions.fill_instructions(session, observations=[observation],
                                             values=values, allow_credentials=True)
        assert [i["name"] for i in allowed] == ["password", "confirm_password"]
        assert all(i["classification"] == "credential" for i in allowed)
        refused = sessions.fill_instructions(session, observations=[observation], values=values)
        assert refused == [], "without the allow flag the planner must not hand out a password"


# --------------------------------------------------------------------------- #
# 4. Layer three — the driver's own re-check
# --------------------------------------------------------------------------- #
class _FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def fill(self, value, timeout=None):
        self._page.typed.append((self._selector, value))


class _FakePage:
    def __init__(self):
        self.typed = []

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def is_closed(self):
        return False


class TestDriverReCheck:
    def _driver(self, session, *, allow_credentials: bool):
        driver = assisted_fill.PlaywrightDriver(session, allow_credentials=allow_credentials)
        driver._page = _FakePage()

        async def observe():
            return _signup_page()

        driver.observe = observe  # type: ignore[method-assign]
        return driver

    def test_password_instruction_is_refused_without_the_opt_in(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        _, session = _start(db, job)
        driver = self._driver(session, allow_credentials=False)
        rows = asyncio.run(driver.fill([{"name": "password", "value": "s3cret"}]))
        assert rows[0]["status"] == "refused_not_autofillable"
        assert driver._page.typed == [], "the default must never reach the page"

    def test_password_instruction_types_with_the_opt_in(self, db, owner, profile):
        job = _job(db, db.query(User).order_by(User.id).first().id)
        _, session = _start(db, job)
        driver = self._driver(session, allow_credentials=True)
        rows = asyncio.run(driver.fill([{"name": "password", "value": "s3cret"}]))
        assert rows[0]["status"] == "filled"
        assert driver._page.typed == [('[name="password"]', "s3cret")]


# --------------------------------------------------------------------------- #
# 5. The pass itself: creates the credential, types it, keeps it out of state
# --------------------------------------------------------------------------- #
class TestOptedInPass:
    def test_a_sign_up_page_gets_a_generated_vault_password_typed(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id)
        _, session = _start(db, job)
        driver = RecordingDriver(observations=[_signup_page()])
        result = asyncio.run(assisted_fill.run_pass(
            db, user=user, session=session, driver=driver, policy=_policy(create_accounts=True)))

        # The vault now holds exactly one credential for this portal, created
        # here (origin auto, username = the profile email)…
        entry = get_vault_entry_for_domain(db, user.id, "jobs.lever.co")
        assert entry is not None
        assert entry.username == "ada@example.com"
        password = reveal_password(entry, db=db)
        # …and that is what the driver was asked to type — both fields.
        assert [f["name"] for f in driver.fills] == ["password", "confirm_password"]
        assert all(f["value"] == password for f in driver.fills)
        assert result["filled"] == ["password", "confirm_password"]

        # The password exists in no persisted session state.
        db.refresh(session)
        blob = json.dumps({"fill_values": session.fill_values, "checkpoint": session.checkpoint})
        assert password not in blob
        fields = (session.checkpoint or {}).get("fields") or {}
        for name in ("password", "confirm_password"):
            assert fields[name]["status"] == "filled"
            assert fields[name]["classification"] == "credential"
            assert fields[name].get("value_fingerprint"), "a fingerprint, never the value"
        # The journal names the field and nothing else.
        steps = json.dumps((session.checkpoint or {}).get("steps") or [])
        assert '"password"' in steps
        assert password not in steps

        # The existing export already carries the login — no new export path.
        exported = export_chrome_csv(list_vault_entries(db, user.id)).text
        assert "ada@example.com" in exported and password in exported
        assert "jobs.lever.co" in exported

        # The event journal says an account credential was created — domain only.
        from app.models.models import JobEvent
        event = (db.query(JobEvent).filter(JobEvent.job_id == job.id,
                                           JobEvent.stage == "credential_created").one())
        assert "jobs.lever.co" in (event.message or "")
        assert password not in json.dumps(event.meta or {})

    def test_a_second_pass_never_types_the_password_again(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id)
        _, session = _start(db, job)
        first = RecordingDriver(observations=[_signup_page()])
        asyncio.run(assisted_fill.run_pass(
            db, user=user, session=session, driver=first, policy=_policy(create_accounts=True)))
        second = RecordingDriver(observations=[_signup_page()])
        asyncio.run(assisted_fill.run_pass(
            db, user=user, session=session, driver=second, policy=_policy(create_accounts=True)))
        assert second.fills == [], "the checkpoint owns 'never typed twice' for credentials too"

    def test_without_the_opt_in_the_same_page_pauses_for_the_human(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id)
        _, session = _start(db, job)
        driver = RecordingDriver(observations=[_signup_page()])
        result = asyncio.run(assisted_fill.run_pass(
            db, user=user, session=session, driver=driver, policy=_policy()))
        assert result["pause"] == "login"
        assert driver.fills == []
        assert get_vault_entry_for_domain(db, user.id, "jobs.lever.co") is None, \
            "no opt-in means no credential is created as a side effect"

    def test_creation_respects_the_auto_create_credentials_switch(self, db, owner, profile):
        user = db.query(User).order_by(User.id).first()
        set_setting(db, user.id, "application", "auto_create_credentials", False)
        db.commit()
        job = _job(db, user.id)
        _, session = _start(db, job)
        driver = RecordingDriver(observations=[_signup_page()])
        result = asyncio.run(assisted_fill.run_pass(
            db, user=user, session=session, driver=driver, policy=_policy(create_accounts=True)))
        # Nothing stored, nothing typed: the honest login pause stays.
        assert result["pause"] == "login"
        assert driver.fills == []
        assert get_vault_entry_for_domain(db, user.id, "jobs.lever.co") is None

    def test_an_existing_vault_login_is_reused_not_recreated(self, db, owner, profile):
        from app.services.vault import credential_for_application

        user = db.query(User).order_by(User.id).first()
        credential, created = credential_for_application(db, user_id=user.id,
                                                         domain="jobs.lever.co", company="Acme")
        assert created is True
        job = _job(db, user.id)
        _, session = _start(db, job)
        driver = RecordingDriver(observations=[_signup_page()])
        asyncio.run(assisted_fill.run_pass(
            db, user=user, session=session, driver=driver, policy=_policy(create_accounts=True)))
        assert [f["value"] for f in driver.fills] == [credential["password"]] * 2
        entries = [e for e in list_vault_entries(db, user.id) if e.domain == "jobs.lever.co"]
        assert len(entries) == 1, "one portal domain, one credential — reused across passes"


# --------------------------------------------------------------------------- #
# 6. The AI accuracy opt-in
# --------------------------------------------------------------------------- #
class TestAiAssistOptIn:
    @pytest.fixture()
    def detected(self, monkeypatch):
        calls = []

        async def fake_detect(url, source=None, *, use_ai=False, **kwargs):
            calls.append({"url": url, "use_ai": use_ai})
            return {"detection_source": "html", "fields": [], "requires_login": False,
                    "portal_type": "lever"}

        monkeypatch.setattr("app.services.apply_flow.detect_form_structure", fake_detect)
        return calls

    async def test_off_by_default(self, db, owner, profile, detected):
        from app.services.apply_flow import prepare_form_plan

        user = db.query(User).order_by(User.id).first()
        job = _job(db, user.id, url="https://jobs.lever.co/acme/ai-off")
        await prepare_form_plan(db, user, job)
        assert detected and detected[-1]["use_ai"] is False

    async def test_user_opt_in_flips_the_flag(self, db, owner, profile, detected):
        from app.services.apply_flow import prepare_form_plan

        user = db.query(User).order_by(User.id).first()
        set_setting(db, user.id, "browser", "ai_assist", True)
        db.commit()
        job = _job(db, user.id, url="https://jobs.lever.co/acme/ai-on")
        await prepare_form_plan(db, user, job)
        assert detected and detected[-1]["use_ai"] is True

    async def test_server_ceiling_beats_the_user(self, db, owner, profile, detected, monkeypatch):
        from app.services.apply_flow import prepare_form_plan

        user = db.query(User).order_by(User.id).first()
        set_setting(db, user.id, "browser", "ai_assist", True)
        db.commit()
        monkeypatch.setattr(settings, "browser_ai_assist_enabled", False)
        job = _job(db, user.id, url="https://jobs.lever.co/acme/ai-ceiling")
        await prepare_form_plan(db, user, job)
        assert detected and detected[-1]["use_ai"] is False
