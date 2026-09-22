"""
Browser-assisted application sessions: lifecycle, pauses, checkpoints, expiry.

The task's acceptance criteria, one test group each:

* a paused form resumes (:func:`test_pause_resume_does_not_duplicate_fields`);
* CAPTCHA and MFA create *actionable* queue items and the user completes the
  challenge in the browser, not through the API
  (:func:`test_captcha_and_mfa_are_actionable_queue_items`);
* resuming does not duplicate already-completed fields
  (:func:`test_resume_skips_completed_fields_and_unknown_stops_the_run`);
* expired sessions require re-authentication safely
  (:func:`test_expired_session_requires_reauthentication`);
* unknown fields stop the automation
  (:func:`test_unknown_field_stops_the_automation`);
* duplicate submissions are impossible
  (:func:`test_duplicate_submission_is_refused_from_the_ledger`).

Plus the privacy rules: no secret in a payload, no secret in a log, no value in
a checkpoint, no plaintext cookie jar on disk, and no screenshot of a screen
where a human is typing a password.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.security import decrypt_secret, encrypt_secret
from app.models.models import (
    ApplicationAction,
    ApplicationSession,
    ApplicationSubmission,
    Job,
    PipelineJob,
    Profile,
    User,
)
from app.services import assisted_fill, job_queue
from app.services import browser_session as sessions
from app.services.assisted_fill import RecordingDriver


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _profile_payload() -> dict:
    return {
        "name": "Ada Lovelace",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "email": "ada@example.com",
        "phone": "+49 30 1234567",
        "location": "Berlin",
        "linkedin": "https://linkedin.com/in/ada",
    }


@pytest.fixture()
def profile(db, owner, auth):
    row = Profile(user_id=db.query(User).order_by(User.id).first().id, data=_profile_payload(), layout={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_job(db, *, user_id: int, url: str = "https://jobs.lever.co/acme/1234",
              company: str = "Acme", external_id: str = "req-1234",
              fields: list | None = None) -> Job:
    job = Job(
        user_id=user_id,
        title="Senior Backend Engineer",
        company=company,
        location="Remote",
        description="Python, FastAPI, PostgreSQL.",
        url=url,
        source="lever",
        external_id=external_id,
        dedupe_key=f"test:{url}:{datetime.utcnow().timestamp()}",
        status="discovered",
        score=80.0,
        extra={"forms": {"portal_type": "lever", "requires_login": False,
                         "vault_domain": "jobs.lever.co", "detection_source": "html",
                         "fields": fields or []}},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _observation(*, url: str = "https://jobs.lever.co/acme/1234", employer: str = "Acme",
                 fields: list | None = None, markers: list | None = None,
                 application_identity: str = "req-1234") -> dict:
    return {
        "url": url,
        "host": url.split("/")[2],
        "employer": employer,
        "application_identity": application_identity,
        "markers": markers or [],
        "fields": fields or [],
    }


def _field(name: str, label: str, field_type: str = "text", *, required: bool = True,
           options: list | None = None) -> dict:
    return {"name": name, "label": label, "type": field_type, "required": required,
            "options": options or []}


# --------------------------------------------------------------------------- #
# Field classification
# --------------------------------------------------------------------------- #
class TestFieldClassification:
    """Unknown, ambiguous, legal, credential and CAPTCHA fields are all caught."""

    def test_unknown_field_asks_and_never_guesses(self):
        from app.services.field_classifier import classify_field

        verdict = classify_field(_field("q_17", "Which of our products excites you most?"))
        assert verdict.classification == "unknown"
        assert verdict.action == "ask_user"
        assert verdict.pauses is True
        assert verdict.profile_key is None

    def test_known_field_with_value_is_autofillable(self):
        from app.services.field_classifier import classify_field

        verdict = classify_field(_field("first_name", "First name"), value="Ada")
        assert verdict.classification == "canonical"
        assert verdict.action == "autofill"
        assert verdict.pauses is False

    def test_password_is_a_handoff_and_never_autofilled(self):
        from app.services.field_classifier import classify_field

        verdict = classify_field(_field("password", "Password", "password"))
        assert verdict.classification == "credential"
        assert verdict.action == "handoff"
        assert verdict.pause_kind == "login"

    def test_mfa_code_is_a_handoff(self):
        from app.services.field_classifier import classify_field

        for name, label in (("otp", "Verification code"), ("mfa_code", "One-time code"),
                            ("sms", "SMS code")):
            verdict = classify_field(_field(name, label))
            assert verdict.classification == "mfa", (name, verdict)
            assert verdict.action == "handoff"
            assert verdict.pause_kind == "mfa"

    def test_captcha_is_detected_and_handed_off(self):
        from app.services.field_classifier import classify_field, is_captcha_marker

        verdict = classify_field({"name": "cf-turnstile-response", "label": "Verify you are human",
                                  "type": "text", "classes": ["cf-turnstile"]})
        assert verdict.classification == "captcha"
        assert verdict.action == "handoff"
        assert verdict.pause_kind == "captcha"
        assert is_captcha_marker("https://www.google.com/recaptcha/api.js") is True

    def test_legal_and_eligibility_questions_pause(self):
        from app.services.field_classifier import classify_field

        for label in ("Are you legally authorized to work in the United States?",
                      "Will you now or in the future require sponsorship?",
                      "I certify that the information provided is accurate",
                      "Do you consent to a background check?"):
            verdict = classify_field(_field("q", label))
            assert verdict.classification == "legal", label
            assert verdict.action == "ask_user"
            assert verdict.pause_kind == "legal_question"

    def test_eeo_questions_are_asked_never_inferred(self):
        from app.services.field_classifier import classify_field

        verdict = classify_field(_field("gender", "Gender (voluntary self-identification)", "select"))
        assert verdict.classification == "voluntary"
        assert verdict.action == "ask_user"
        assert verdict.sensitivity == "restricted"

    def test_restricted_identifiers_are_never_filled(self):
        from app.services.field_classifier import classify_field

        verdict = classify_field(_field("ssn", "Social Security Number"))
        assert verdict.sensitivity == "restricted"
        assert verdict.action == "never"
        assert verdict.autofillable is False

    def test_ambiguous_label_stops_instead_of_guessing(self):
        from app.services.field_classifier import classify_field

        verdict = classify_field(_field("loc", "Location"), value="Berlin")
        assert verdict.classification == "ambiguous"
        assert verdict.action == "ask_user"
        assert verdict.reason == "ambiguous_label"

    def test_ambiguous_option_stops(self):
        from app.services.field_classifier import classify_field

        verdict = classify_field(_field("auth", "Work authorization", "select",
                                        options=["Yes", "No"]), value="Yes, with sponsorship")
        assert verdict.classification == "ambiguous"
        assert verdict.reason == "ambiguous_option"

    def test_already_completed_field_is_skipped(self):
        from app.services.field_classifier import classify_field

        verdict = classify_field(_field("first_name", "First name"), value="Ada",
                                 checkpoint_status="filled")
        assert verdict.action == "skip"
        assert verdict.blocks is False

    def test_form_pause_priority_prefers_handoffs(self):
        from app.services.field_classifier import classify_form

        form = classify_form([
            _field("q_1", "Anything else you want us to know?"),
            {"name": "captcha", "label": "Verify you are human", "type": "text",
             "classes": ["g-recaptcha"], "required": True},
        ])
        assert form.must_pause is True
        assert form.pause_kind == "captcha"
        assert form.summary()["requires_browser_handoff"] is True


# --------------------------------------------------------------------------- #
# Lifecycle, pauses and the action queue
# --------------------------------------------------------------------------- #
class TestSessionLifecycle:
    def test_start_is_single_live_session_per_job_and_isolated(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, created = sessions.start_session(db, user=user, job=job)
        same, created_again = sessions.start_session(db, user=user, job=job)
        assert created is True and created_again is False
        assert same.id == session.id
        assert session.isolation_key.startswith(f"u{user.id}-")
        assert session.browser_profile_ref.startswith(f"u{user.id}/")
        assert sessions.profile_dir(session).startswith(settings.browser_profile_path())

    def test_profile_path_cannot_escape_its_root(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        session.browser_profile_ref = "../../etc"
        with pytest.raises(sessions.SessionError) as excinfo:
            sessions.profile_dir(session)
        assert excinfo.value.code == "isolation_violation"

    def test_another_users_session_is_not_found(self, db, owner, member, auth, member_auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        other = db.query(User).filter(User.id != user.id).first()
        with pytest.raises(sessions.SessionNotFound):
            sessions.own_session(db, other, session.id)

    def test_unknown_field_stops_the_automation(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id, fields=[_field("first_name", "First name")])
        session, _ = sessions.start_session(db, user=user, job=job)
        outcome = sessions.record_observation(
            db, session, _observation(fields=[_field("mystery", "Favourite colour?")]),
            user=user, values={"first_name": "Ada"},
        )
        assert outcome["status"] == "awaiting_user"
        assert outcome["pause"] == "unknown_field"
        assert outcome["to_fill"] == []
        assert session.progress["must_pause"] is True

        action = db.query(ApplicationAction).filter(ApplicationAction.session_id == session.id).one()
        assert action.kind == "unknown_field"
        assert action.status == "pending"
        assert action.fields[0]["label"] == "Favourite colour?"
        # The queue is the actionable surface.
        queue = sessions.action_queue(db, user.id)
        assert [entry["id"] for entry in queue] == [action.id]

    def test_captcha_and_mfa_are_actionable_queue_items(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)

        outcome = sessions.record_observation(
            db, session, _observation(markers=["captcha"], fields=[_field("g-recaptcha-response",
                                                                        "Verify you are human")]),
            user=user,
        )
        assert outcome["pause"] == "captcha"
        assert outcome["requires_browser_handoff"] is True
        captcha_action = db.query(ApplicationAction).filter(ApplicationAction.kind == "captcha").one()
        descriptor = sessions.issue_handoff(db, user=user, session=session, action=captcha_action)
        assert descriptor["token"].startswith("ho_")
        assert descriptor["user_completes_in_browser"] is True
        assert any("CAPTCHA" in line for line in descriptor["never"])
        # Only the hash is stored; the raw token is not recoverable from the row.
        assert session.handoff_token_hash != descriptor["token"]
        assert sessions.consume_handoff_token(db, session, descriptor["token"]) is True
        assert sessions.consume_handoff_token(db, session, descriptor["token"]) is False  # single use

        # Second factor: same flow, a different, equally actionable kind.
        sessions.complete_action(db, user=user, action=captcha_action)
        outcome = sessions.record_observation(
            db, session, _observation(markers=["mfa"], fields=[_field("otp", "Verification code")]),
            user=user,
        )
        assert outcome["pause"] == "mfa"
        mfa_action = db.query(ApplicationAction).filter(ApplicationAction.kind == "mfa").one()
        assert mfa_action.status == "pending"
        assert "never sent to us" in mfa_action.instructions

    def test_ending_a_session_drops_the_values_it_could_have_typed(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(
            db, session, _observation(fields=[_field("first_name", "First name")]),
            user=user, values={"first_name": "Ada"},
        )
        assert session.fill_values == {"first_name": "Ada"}
        sessions.cancel_session(db, user=user, session=session, reason="user_cancelled")
        assert session.fill_values == {}

    def test_expiry_drops_the_working_set_too(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(
            db, session, _observation(fields=[_field("first_name", "First name")]),
            user=user, values={"first_name": "Ada"},
        )
        sessions.expire_session(db, session, reason="ttl_elapsed")
        assert session.fill_values == {}
        assert session.state == "expired"

    def test_action_completion_rejects_secrets(self, client, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(markers=["mfa"]), user=user)
        action = db.query(ApplicationAction).filter(ApplicationAction.kind == "mfa").one()

        response = client.post(
            f"/api/application-sessions/{session.id}/actions/{action.id}/complete",
            json={"answers": {"otp": "123456"}}, headers=auth,
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "restricted_field"
        # Nothing was stored.
        db.expire_all()
        assert db.query(ApplicationAction).filter(ApplicationAction.id == action.id).one().status == "pending"

    def test_completed_login_item_returns_the_session_to_paused(self, client, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(markers=["login"]), user=user)
        action = db.query(ApplicationAction).filter(ApplicationAction.kind == "login").one()

        response = client.post(
            f"/api/application-sessions/{session.id}/actions/{action.id}/complete",
            json={"note": "signed in"}, headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["state"] == "paused"
        assert response.json()["checkpoint"]["steps"] >= 1


# --------------------------------------------------------------------------- #
# Pause / resume and duplicate-free filling
# --------------------------------------------------------------------------- #
class TestPauseResume:
    def test_pause_resume_does_not_duplicate_fields(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)

        # 1. First observation: two safe fields + one unknown → the run pauses.
        outcome = sessions.record_observation(
            db, session,
            _observation(fields=[_field("first_name", "First name"), _field("email", "Email"),
                                 _field("mystery", "How did you hear about us?")]),
            user=user, values={"first_name": "Ada", "email": "ada@example.com"},
        )
        assert outcome["pause"] == "unknown_field"

        # 2. The assistant fills what it may, and records it (fingerprints only).
        instructions = sessions.fill_instructions(
            session, values={"first_name": "Ada", "email": "ada@example.com"})
        assert {i["name"] for i in instructions} == {"first_name", "email"}
        sessions.record_fills(db, session, [{"name": "first_name", "value": "Ada"},
                                            {"name": "email", "value": "ada@example.com"}])
        filled_once = sessions.already_completed_fields(session)
        assert filled_once == ["email", "first_name"]

        # 3. The user answers the unknown field and resumes.
        action = db.query(ApplicationAction).filter(ApplicationAction.kind == "unknown_field").one()
        sessions.record_user_answers(db, session, {"mystery": "Job board"})
        sessions.complete_action(db, user=user, action=action)
        outcome = sessions.resume_session(
            db, user=user, session=session,
            observed=_observation(fields=[_field("first_name", "First name"), _field("email", "Email"),
                                          _field("mystery", "How did you hear about us?")]),
        )
        assert outcome["status"] in ("active", "completed")
        # The user's in-app answer is the only thing left to type; the fields we
        # already filled are not in the plan again.
        assert outcome["to_fill"] == ["mystery"]
        assert "first_name" not in outcome["to_fill"] and "email" not in outcome["to_fill"]

        # 4. A second pass re-observing the same page types the answer and still
        # will not re-type anything already done.
        remaining = sessions.fill_instructions(
            session, values={"first_name": "Ada", "email": "ada@example.com", "mystery": "Job board"})
        assert [i["name"] for i in remaining] == ["mystery"]
        sessions.record_fills(db, session, [{"name": "mystery", "value": "Job board"}])
        assert sessions.already_completed_fields(session) == ["email", "first_name", "mystery"]
        assert session.progress["filled"] == 3
        # The checkpoint proves what happened without holding what was typed.
        stored = json.dumps(session.checkpoint)
        for value in ("Ada", "ada@example.com", "Job board"):
            assert value not in stored
        assert session.checkpoint["fields"]["email"]["value_fingerprint"]

    def test_resume_refuses_when_the_url_moved(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(fields=[_field("first_name", "First name")]),
                                    user=user, values={"first_name": "Ada"})

        with pytest.raises(sessions.CheckpointFailed) as excinfo:
            sessions.resume_session(db, user=user, session=session,
                                    observed=_observation(url="https://evil.example.com/apply"))
        assert "url_changed" in excinfo.value.detail["codes"]
        assert session.last_checkpoint_failure == "url_changed"
        # A refused resume keeps the session paused and tells the user why.
        db.expire_all()
        refreshed = db.query(ApplicationSession).filter(ApplicationSession.id == session.id).one()
        assert refreshed.state == "awaiting_user"
        reason_action = (
            db.query(ApplicationAction)
            .filter(ApplicationAction.session_id == session.id, ApplicationAction.status == "pending")
            .order_by(ApplicationAction.id.desc())
            .first()
        )
        assert reason_action is not None and reason_action.reason == "url_changed"

    def test_resume_refuses_on_employer_and_identity_mismatch(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(fields=[_field("first_name", "First name")]),
                                    user=user, values={"first_name": "Ada"})

        failures = sessions.checkpoint_failures(
            db, session,
            observed=_observation(employer="Globex", application_identity="req-9999"),
        )
        codes = {failure["code"] for failure in failures}
        assert "employer_mismatch" in codes
        assert "application_identity_mismatch" in codes

    def test_resume_refuses_when_the_job_moved_to_another_account(self, db, owner, member, auth,
                                                                 member_auth, profile):
        user = db.query(User).order_by(User.id).first()
        other = db.query(User).filter(User.id != user.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(fields=[_field("first_name", "First name")]),
                                    user=user, values={"first_name": "Ada"})
        job.user_id = other.id
        db.commit()
        failures = sessions.checkpoint_failures(db, session)
        assert {f["code"] for f in failures} == {"job_not_owned"}

    def test_user_pause_and_resume_round_trip(self, client, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        start = client.post("/api/application-sessions", json={"job_id": job.id}, headers=auth)
        assert start.status_code == 200, start.text
        session_id = start.json()["session"]["id"]

        paused = client.post(f"/api/application-sessions/{session_id}/pause", headers=auth)
        assert paused.status_code == 200, paused.text
        assert paused.json()["state"] == "paused"

        resumed = client.post(f"/api/application-sessions/{session_id}/resume", json={}, headers=auth)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "active"
        assert resumed.json()["resumed_count"] == 1

    def test_observation_never_stores_values_or_page_text(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(
            db, session,
            {
                "url": "https://jobs.lever.co/acme/1234",
                "html": "<html>secret page source</html>",
                "text": "confidential posting text",
                "fields": [{"name": "email", "label": "Email", "type": "email",
                            "value": "ada@example.com", "required": True}],
            },
            user=user, values={"email": "ada@example.com"},
        )
        stored = json.dumps(session.last_observation)
        assert "secret page source" not in stored
        assert "confidential posting text" not in stored
        assert "ada@example.com" not in stored
        assert session.last_observation["fields"][0]["value_present"] is True

        checkpoint = json.dumps(session.checkpoint)
        assert "ada@example.com" not in checkpoint  # only a fingerprint ever lands there


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
class TestExpiry:
    def test_expired_session_requires_reauthentication(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(fields=[_field("first_name", "First name")]),
                                    user=user, values={"first_name": "Ada"})
        # Persist state first, so expiry has something to purge.
        from app.services.user_settings import set_setting

        set_setting(db, user.id, "browser", "persist_session", True)
        db.commit()
        sessions.persist_storage_state(db, session, {"cookies": [
            {"name": "session", "value": "abc", "domain": "jobs.lever.co", "path": "/"}]})
        assert session.storage_state_enc

        session.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
        assert sessions.is_expired(session) is True

        with pytest.raises(sessions.SessionExpired):
            sessions.resume_session(db, user=user, session=session)
        db.expire_all()
        expired = db.query(ApplicationSession).filter(ApplicationSession.id == session.id).one()
        assert expired.state == "expired"
        assert expired.storage_state_enc is None
        assert expired.storage_state_purged_at is not None
        open_action = db.query(ApplicationAction).filter(ApplicationAction.session_id == session.id).all()
        assert all(a.status in ("expired", "completed") for a in open_action)

        # Re-authentication is the way back: fresh TTL, fresh login handoff.
        outcome = sessions.reauthenticate(db, user=user, session=expired)
        assert outcome["requires_login"] is True
        assert expired.state == "awaiting_user"
        assert expired.expires_at > datetime.utcnow()
        login_action = db.query(ApplicationAction).filter(ApplicationAction.kind == "login").one()
        assert login_action.status == "pending"

    def test_sweep_expires_and_reports(self, client, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        session.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
        assert sessions.sweep_expired_sessions(db, user_id=user.id) == 1
        listed = client.get("/api/application-sessions", headers=auth).json()
        assert listed["sessions"] == [] or listed["sessions"][0]["state"] == "expired"
        stats = client.get("/api/application-sessions/stats", headers=auth).json()
        assert stats["by_state"].get("expired") == 1

    def test_session_limit_is_enforced(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        from app.services.user_settings import set_setting

        set_setting(db, user.id, "browser", "max_live_sessions", 1)
        db.commit()
        first = _make_job(db, user_id=user.id, url="https://jobs.lever.co/acme/1", external_id="r1")
        second = _make_job(db, user_id=user.id, url="https://jobs.lever.co/acme/2", external_id="r2")
        sessions.start_session(db, user=user, job=first)
        with pytest.raises(sessions.SessionError) as excinfo:
            sessions.start_session(db, user=user, job=second)
        assert excinfo.value.code == "session_limit_reached"


# --------------------------------------------------------------------------- #
# Encrypted, optional persistence
# --------------------------------------------------------------------------- #
class TestPersistence:
    STATE = {"cookies": [{"name": "sid", "value": "super-secret-cookie",
                          "domain": "jobs.lever.co", "path": "/", "httpOnly": True,
                          "secure": True, "sameSite": "Lax"}],
             "origins": [{"origin": "https://jobs.lever.co",
                          "localStorage": [{"name": "theme", "value": "dark"},
                                           {"name": "password", "value": "hunter2"}]}]}

    def test_persistence_is_off_until_the_user_opts_in(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        result = sessions.persist_storage_state(db, session, self.STATE)
        assert result == {"persisted": False, "reason": "persistence_disabled_by_user"}
        assert session.storage_state_enc is None

    def test_persisted_state_is_encrypted_with_the_users_own_key(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        from app.services.user_settings import set_setting

        set_setting(db, user.id, "browser", "persist_session", True)
        db.commit()
        result = sessions.persist_storage_state(db, session, self.STATE)
        assert result["persisted"] is True

        blob = session.storage_state_enc
        assert "super-secret-cookie" not in blob          # ciphertext, not JSON
        assert "hunter2" not in blob
        decrypted = decrypt_secret(blob, sessions.storage_state_scope(user.id))
        state = json.loads(decrypted)
        assert state["cookies"][0]["name"] == "sid"
        # A password-looking localStorage entry is dropped, never persisted.
        names = [entry["name"] for entry in state["origins"][0]["localStorage"]]
        assert names == ["theme"]

        # The user's own key is required: another scope cannot read it.
        with pytest.raises(InvalidToken):
            decrypt_secret(blob, sessions.storage_state_scope(user.id + 1))

        loaded = sessions.load_storage_state(db, session, user=user)
        assert loaded["cookies"][0]["value"] == "super-secret-cookie"

    def test_state_is_purged_on_cancel_and_not_loadable_after(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        from app.services.user_settings import set_setting

        set_setting(db, user.id, "browser", "persist_session", True)
        db.commit()
        sessions.persist_storage_state(db, session, self.STATE)
        sessions.cancel_session(db, user=user, session=session)
        assert session.storage_state_enc is None
        assert sessions.load_storage_state(db, session, user=user) is None

    def test_another_user_cannot_load_the_state(self, db, owner, member, auth, member_auth, profile):
        user = db.query(User).order_by(User.id).first()
        other = db.query(User).filter(User.id != user.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        from app.services.user_settings import set_setting

        set_setting(db, user.id, "browser", "persist_session", True)
        db.commit()
        sessions.persist_storage_state(db, session, self.STATE)
        with pytest.raises(sessions.SessionNotFound):
            sessions.load_storage_state(db, session, user=other)


# --------------------------------------------------------------------------- #
# Screenshots
# --------------------------------------------------------------------------- #
class TestScreenshotPolicy:
    def test_handoff_screens_are_never_captured(self):
        policy = sessions.SessionPolicy(screenshots_enabled=True, screenshot_retention_days=7)
        for kind in ("login", "mfa", "captcha"):
            decision = sessions.screenshot_decision(policy, kind=kind)
            assert decision["allowed"] is False
            assert decision["reason"] == "handoff_screen_never_captured"

    def test_capture_requires_opt_in_and_a_retention_window(self):
        off = sessions.SessionPolicy(screenshots_enabled=False, screenshot_retention_days=7)
        assert sessions.screenshot_decision(off, kind="field_fill")["reason"] == "screenshots_not_enabled"
        zero = sessions.SessionPolicy(screenshots_enabled=True, screenshot_retention_days=0)
        assert sessions.screenshot_decision(zero, kind="field_fill")["reason"] == "retention_window_zero"
        on = sessions.SessionPolicy(screenshots_enabled=True, screenshot_retention_days=3)
        assert sessions.screenshot_decision(on, kind="field_fill")["allowed"] is True

    def test_retention_is_clamped_to_the_deployment_ceiling(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        from app.services.user_settings import set_setting

        set_setting(db, user.id, "browser", "screenshots_enabled", True)
        set_setting(db, user.id, "browser", "screenshot_retention_days", 365)
        db.commit()
        policy = sessions.effective_policy(db, user.id)
        assert policy.screenshot_retention_days <= settings.browser_screenshot_max_retention_days


# --------------------------------------------------------------------------- #
# Duplicate submissions
# --------------------------------------------------------------------------- #
class TestDuplicateSubmissions:
    def test_duplicate_submission_is_refused_from_the_ledger(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        # A submission is only ever considered from a session whose checkpoint
        # actually validated against the page (no observation, no submission).
        sessions.record_observation(db, session, _observation(), user=user)
        assert sessions.checkpoint_failures(db, session) == []

        # Auto-submit is off in this deployment/task: the first refusal says so.
        row, decision = sessions.reserve_submission(db, user=user, job=job, session=session)
        assert row is None
        assert decision["reason"] == "policy_disallows_submit"

        # With the deployment gates open (and the user's consent), a retry is
        # still refused once the ledger holds a live submission.
        policy = sessions.SessionPolicy(allow_submit=True, dry_run=False)
        first, decision = sessions.reserve_submission(db, user=user, job=job, session=session, policy=policy)
        assert first is not None and decision["allowed"] is True
        second, decision = sessions.reserve_submission(db, user=user, job=job, session=session, policy=policy)
        assert second is None
        assert decision["reason"] == "already_submitted"
        assert decision["submission_id"] == first.id

        # The database agrees, even if a caller bypasses the service check.
        db.add(ApplicationSubmission(user_id=user.id, job_id=job.id, state="submitted",
                                     channel="automation", idempotency_key="other", dry_run=False))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_submit_endpoint_reports_the_refusal_typed(self, client, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        started = client.post("/api/application-sessions", json={"job_id": job.id}, headers=auth)
        session_id = started.json()["session"]["id"]
        response = client.post(f"/api/application-sessions/{session_id}/submit", headers=auth)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "application_not_submittable"
        assert response.json()["detail"]["reason"] == "policy_disallows_submit"

    def test_an_expired_session_cannot_submit(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        session.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
        row, decision = sessions.reserve_submission(
            db, user=user, job=job, session=session,
            policy=sessions.SessionPolicy(allow_submit=True, dry_run=False))
        assert row is None and decision["reason"] == "session_expired"

    def test_pending_action_blocks_submission(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(markers=["captcha"]), user=user)
        row, decision = sessions.reserve_submission(
            db, user=user, job=job, session=session,
            policy=sessions.SessionPolicy(allow_submit=True, dry_run=False))
        assert row is None and decision["reason"] == "action_required"


# --------------------------------------------------------------------------- #
# The pass loop (recording driver)
# --------------------------------------------------------------------------- #
class TestAssistedPass:
    def test_direct_pass_uses_assisted_runtime_when_auto_apply_is_disabled(self, client, db, owner, auth, profile, monkeypatch):
        """The direct HTTP route must share the queue's independent gate."""
        from app.services import autofill

        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        monkeypatch.setattr(settings, "autofill_enabled", False)
        monkeypatch.setattr(autofill, "assisted_apply_available", lambda: {"available": True, "dry_run": True})

        async def fake_run_pass(*_args, **_kwargs):
            return {"status": "filled", "filled": ["first_name"], "submitted": False}

        monkeypatch.setattr(assisted_fill, "run_pass", fake_run_pass)
        response = client.post(f"/api/application-sessions/{session.id}/pass", json={}, headers=auth)

        assert response.status_code == 200, response.text
        assert response.json()["pass"]["status"] == "filled"

    def test_pass_fills_safe_fields_and_stops_at_the_captcha(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        job.extra = dict(job.extra or {})
        job.extra["autofill_plan"] = {"fields": [
            {"name": "first_name", "profile_key": "firstName", "type": "text", "value": "Ada"},
            {"name": "email", "profile_key": "email", "type": "email", "value": "ada@example.com"},
            {"name": "password", "profile_key": None, "type": "password", "value": "vault-secret",
             "value_source": "vault"},
        ]}
        db.commit()

        driver = RecordingDriver(observations=[
            _observation(fields=[_field("first_name", "First name"), _field("email", "Email"),
                                 _field("password", "Password", "password")]),
        ])
        result = asyncio.run(assisted_fill.run_pass(db, user=user, session=session, driver=driver))
        assert driver.opened is True and driver.closed is True
        filled = {fill["name"] for fill in driver.fills}
        assert filled == {"first_name", "email"}
        # The vault password never reaches the driver, even though the plan holds it.
        assert all("vault-secret" not in json.dumps(fill) for fill in driver.fills)
        assert result["pause"] == "login"
        assert session.state == "awaiting_user"

    def test_pass_second_run_does_not_retype_completed_fields(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        job.extra = dict(job.extra or {})
        job.extra["autofill_plan"] = {"fields": [
            {"name": "first_name", "profile_key": "firstName", "type": "text", "value": "Ada"}]}
        db.commit()

        page = _observation(fields=[_field("first_name", "First name")])
        first_driver = RecordingDriver(observations=[dict(page)])
        asyncio.run(assisted_fill.run_pass(db, user=user, session=session, driver=first_driver))
        assert first_driver.fills and first_driver.fills[0]["name"] == "first_name"

        second_driver = RecordingDriver(observations=[dict(page)])
        asyncio.run(assisted_fill.run_pass(db, user=user, session=session, driver=second_driver))
        assert second_driver.fills == []

    def test_plan_is_reviewable_without_a_browser(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(
            db, session,
            _observation(fields=[_field("first_name", "First name"),
                                 _field("why", "Why do you want this role?")]),
            user=user, values={"first_name": "Ada"},
        )
        plan = assisted_fill.plan_for_session(job, session)
        assert plan["autofillable"] == ["first_name"]
        assert [entry["name"] for entry in plan["asking"]] == ["why"]
        assert plan["must_pause"] is True


# --------------------------------------------------------------------------- #
# Privacy: nothing secret in a log or a response
# --------------------------------------------------------------------------- #
class TestNoSecretsLeak:
    def test_credentials_and_codes_never_reach_the_logs(self, db, owner, auth, profile, caplog):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        secret = "sup3r-secret-password"
        code = "918273"
        with caplog.at_level(logging.DEBUG):
            sessions.record_observation(
                db, session,
                _observation(fields=[_field("password", "Password", "password"),
                                     _field("otp", "Verification code")]),
                user=user, values={"password": secret, "otp": code},
            )
        haystack = "\n".join(record.getMessage() for record in caplog.records)
        assert secret not in haystack
        assert code not in haystack

    def test_session_payload_has_no_state_or_token_material(self, client, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        started = client.post("/api/application-sessions", json={"job_id": job.id}, headers=auth)
        payload = json.dumps(started.json())
        assert "storage_state" not in payload
        assert "handoff_token_hash" not in payload
        assert "browser_profile_ref" not in payload
        assert "isolation_key" not in payload
        assert "enc_scope" not in payload

    def test_encrypt_scope_is_per_user(self):
        scope_a = sessions.storage_state_scope(1)
        scope_b = sessions.storage_state_scope(2)
        assert scope_a != scope_b
        blob = encrypt_secret("cookie", scope_a)
        with pytest.raises(InvalidToken):
            decrypt_secret(blob, scope_b)


# --------------------------------------------------------------------------- #
# The durable queue path
# --------------------------------------------------------------------------- #
class TestQueuePipeline:
    """A pass runs in the worker, and a pause is a *result*, not a failure."""

    def test_enqueue_is_idempotent_per_resume_attempt(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)

        first = assisted_fill.enqueue_pass(db, user=user, session=session)
        assert first["queued"] is True and first["duplicate"] is False
        again = assisted_fill.enqueue_pass(db, user=user, session=session)
        assert again["duplicate"] is True and again["item_id"] == first["item_id"]

        # A resume is a new attempt: a *new* pass, not a duplicate of this one.
        session.resumed_count += 1
        db.commit()
        third = assisted_fill.enqueue_pass(db, user=user, session=session)
        assert third["queued"] is True and third["duplicate"] is False
        assert third["item_id"] != first["item_id"]

    def test_queued_pass_is_a_noop_while_the_user_has_an_action(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(markers=["captcha"]), user=user)
        item, _ = job_queue.enqueue_or_existing(
            db, user_id=user.id, pipeline="browser_session", job_id=job.id,
            payload={"session_id": session.id}, dedupe_key=f"browser-session:{session.id}",
        )

        driver = RecordingDriver(observations=[_observation()])
        result = asyncio.run(assisted_fill.run_queued_pass(db, item, driver=driver))
        assert result["noop"] == "awaiting_user"
        assert result["pause"] == "captcha"
        # The browser was never opened while the human still had work to do.
        assert driver.opened is False and driver.fills == []

    def test_queued_pass_reports_unavailable_instead_of_simulating(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        item, _ = job_queue.enqueue_or_existing(
            db, user_id=user.id, pipeline="browser_session", job_id=job.id,
            payload={"session_id": session.id}, dedupe_key=f"browser-session:{session.id}",
        )
        # The interactive runtime has its own explicit operator switch; this
        # test must not treat AUTOFILL_ENABLED as that switch.
        original = settings.assisted_apply_enabled
        settings.assisted_apply_enabled = False
        try:
            result = asyncio.run(assisted_fill.run_queued_pass(db, item))
        finally:
            settings.assisted_apply_enabled = original
        assert result["status"] == "unavailable"
        assert result["noop"] == "assisted_apply_unavailable"

    def test_worker_handler_runs_a_pass_and_keeps_the_pause(self, db, owner, auth, profile):
        from app.services.handlers import HANDLERS

        assert "browser_session" in HANDLERS
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        item, _ = job_queue.enqueue_or_existing(
            db, user_id=user.id, pipeline="browser_session", job_id=job.id,
            payload={"session_id": session.id}, dedupe_key=f"browser-session:{session.id}",
        )
        original = settings.assisted_apply_enabled
        settings.assisted_apply_enabled = False
        try:
            result = asyncio.run(HANDLERS["browser_session"](db, item))
        finally:
            settings.assisted_apply_enabled = original
        assert result["noop"] == "assisted_apply_unavailable" and result["session_id"] == session.id
        # The step vocabulary is the contract the UI's stepper renders.
        assert item.payload["progress"]["step"] == "done"

    def test_a_paused_pass_reports_the_awaiting_user_step(self, db, owner, auth, profile):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        job.extra = dict(job.extra or {})
        job.extra["autofill_plan"] = {"fields": [
            {"name": "first_name", "profile_key": "firstName", "type": "text", "value": "Ada"}]}
        db.commit()
        item, _ = job_queue.enqueue_or_existing(
            db, user_id=user.id, pipeline="browser_session", job_id=job.id,
            payload={"session_id": session.id}, dedupe_key=f"browser-session:{session.id}",
        )
        driver = RecordingDriver(observations=[
            _observation(fields=[_field("first_name", "First name")]),
            _observation(fields=[_field("first_name", "First name")], markers=["captcha"]),
        ])
        result = asyncio.run(assisted_fill.run_queued_pass(db, item, driver=driver))
        assert result["pause"] == "captcha"
        assert item.payload["progress"]["step"] == "awaiting_user"
        assert driver.fills and driver.fills[0]["name"] == "first_name"


# --------------------------------------------------------------------------- #
# Resuming queues the next pass (and only when a browser can actually run)
# --------------------------------------------------------------------------- #
class TestResumeQueuesTheNextPass:
    def test_resume_queues_a_pass_when_automation_is_available(self, client, db, owner, auth, profile,
                                                              monkeypatch):
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(markers=["captcha"]), user=user)
        action = db.query(ApplicationAction).filter(ApplicationAction.kind == "captcha").one()

        from app.services import autofill

        monkeypatch.setattr(autofill, "assisted_apply_available",
                            lambda: {"available": True, "reason": ""})
        completed = client.post(
            f"/api/application-sessions/{session.id}/actions/{action.id}/complete",
            json={"note": "cleared the check"}, headers=auth,
        )
        assert completed.status_code == 200, completed.text
        queued = completed.json().get("continuing_pass")
        assert queued and queued["queued"] is True
        row = db.query(PipelineJob).filter(PipelineJob.id == queued["item_id"]).one()
        assert row.pipeline == "browser_session"
        assert row.payload["session_id"] == session.id

    def test_resume_queues_nothing_when_assisted_apply_is_disabled(self, client, db, owner, auth, profile, monkeypatch):
        monkeypatch.setattr(settings, "assisted_apply_enabled", False)
        user = db.query(User).order_by(User.id).first()
        job = _make_job(db, user_id=user.id)
        session, _ = sessions.start_session(db, user=user, job=job)
        sessions.record_observation(db, session, _observation(markers=["mfa"]), user=user)
        action = db.query(ApplicationAction).filter(ApplicationAction.kind == "mfa").one()
        response = client.post(
            f"/api/application-sessions/{session.id}/actions/{action.id}/complete",
            json={"note": "code entered in the browser"}, headers=auth,
        )
        assert response.status_code == 200
        assert "continuing_pass" not in response.json()
        assert db.query(PipelineJob).filter(PipelineJob.pipeline == "browser_session").count() == 0
