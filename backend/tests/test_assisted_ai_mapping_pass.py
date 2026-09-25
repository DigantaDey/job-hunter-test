"""Pass-loop integration tests for AI field mapping.

These exercise run_pass with the real RecordingDriver and a real
browser_session, asserting the promises from the feature brief:

* When ``policy.ai_assist`` is False (default), the AI is never called.
* When AI is on and returns a good mapping, an otherwise unknown field is
  filled from the profile instead of pausing for unknown_field.
* When AI is on and raises, the pass pauses with ``ai_unavailable`` rather
  than silently filling garbage.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.models import Job, Profile, User
from app.services import assisted_fill
from app.services import browser_session as sessions
from app.services.assisted_fill import RecordingDriver
from app.services.browser_session import SessionPolicy


@pytest.fixture()
def profile(db):
    user = db.query(User).order_by(User.id).first()
    row = Profile(user_id=user.id,
                  data={"name": "Ada Lovelace", "firstName": "Ada", "lastName": "Lovelace",
                        "email": "ada@example.com", "phone": "+49 30 1234567",
                        "location": "Berlin", "linkedin": "https://linkedin.com/in/ada"},
                  layout={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _plan(*fields: tuple[str, str, str]) -> list[dict]:
    return [{"name": name, "profile_key": key, "type": "text", "value": value,
             "value_source": "profile"} for name, key, value in fields]


def _job(db, user_id: int, *, url: str = "https://jobs.lever.co/acme/1234",
         plan: list | None = None) -> Job:
    job = Job(user_id=user_id, title="Senior Backend Engineer", company="Acme", url=url,
              external_id="req-ai", status="ready_to_apply")
    if plan is not None:
        job.extra = {"autofill_plan": {"fields": plan}}
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _page(*fields: dict, url: str = "https://jobs.lever.co/acme/1234",
          title: str = "Apply", **extra) -> dict:
    return {"url": url, "host": "jobs.lever.co", "title": title, "fields": list(fields),
            "forms": [{"fields": [f["name"] for f in fields]}], "markers": [], **extra}


def _field(name: str, label: str, type_: str = "text", **extra) -> dict:
    return {"name": name, "label": label, "type": type_, "required": True, **extra}


def _run(db, user, session, driver, **kwargs):
    return asyncio.run(assisted_fill.run_pass(
        db, user=user, session=session, driver=driver, **kwargs))


def _patch_ai(monkeypatch, response_or_error):
    import app.services.ai_client as ai_client_mod
    if not hasattr(ai_client_mod, "fit_prompt_part"):
        monkeypatch.setattr(ai_client_mod, "fit_prompt_part",
                            lambda s, _b, label=None: (s, True), raising=False)
    if not hasattr(ai_client_mod, "input_budget_chars"):
        monkeypatch.setattr(ai_client_mod, "input_budget_chars", lambda: 8000, raising=False)

    async def fake(_kind, _prompt, **_kw):
        if isinstance(response_or_error, BaseException):
            raise response_or_error
        return response_or_error

    monkeypatch.setattr(ai_client_mod, "chat_completion", fake, raising=False)


def test_ai_not_called_when_opted_out(db, owner, profile, monkeypatch):
    """Default policy has ai_assist=False; chat_completion must not be called."""
    import app.services.ai_client as ai_client_mod
    calls = {"n": 0}

    async def boom(*a, **kw):
        calls["n"] += 1
        raise AssertionError("AI must not be called when opted out")

    monkeypatch.setattr(ai_client_mod, "chat_completion", boom, raising=False)
    if not hasattr(ai_client_mod, "fit_prompt_part"):
        monkeypatch.setattr(ai_client_mod, "fit_prompt_part",
                            lambda s, _b, label=None: (s, True), raising=False)
    if not hasattr(ai_client_mod, "input_budget_chars"):
        monkeypatch.setattr(ai_client_mod, "input_budget_chars", lambda: 8000, raising=False)

    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id)
    session, _ = sessions.start_session(db, user=user, job=job)
    driver = RecordingDriver(observations=[
        _page(_field("qWeird", "How should we address you?"),
              title="Mystery field page")])
    result = _run(db, user, session, driver, policy=SessionPolicy(ai_assist=False))
    db.refresh(session)
    assert calls["n"] == 0
    # Without AI the mystery field stays unknown → pause.
    assert result["pause_reason"] == "unknown_field"


def test_ai_maps_unknown_field_and_it_gets_filled(db, owner, profile, monkeypatch):
    # Make sure the profile value exists under the canonical key the AI maps to,
    # seeded via a plan entry (firstName is a KNOWN_FIELD).
    _patch_ai(monkeypatch, {"mapping": {"qWeird": "firstName"}})
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id, plan=_plan(("firstName", "firstName", "Ada")))
    session, _ = sessions.start_session(db, user=user, job=job)

    # Second observation ends with a signature so the pass has a human gate to
    # land on (otherwise it loops looking for a submit it can't click).
    driver = RecordingDriver(observations=[
        _page(_field("qWeird", "How should we address you?"),
              title="Mystery page"),
        _page(_field("legalSig", "Type your legal signature", signature=True),
              title="Signature"),
    ])
    result = _run(db, user, session, driver, policy=SessionPolicy(ai_assist=True))
    db.refresh(session)
    names_filled = [f["name"] for f in driver.fills]
    assert "qWeird" in names_filled, f"fills={names_filled} result={result}"
    assert session.state == "awaiting_user"


def test_ai_failure_surfaces_as_pause(db, owner, profile, monkeypatch):
    _patch_ai(monkeypatch, RuntimeError("AI is down"))
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id)
    session, _ = sessions.start_session(db, user=user, job=job)
    driver = RecordingDriver(observations=[
        _page(_field("qWeird", "How should we address you?"),
              title="Mystery page")])
    result = _run(db, user, session, driver, policy=SessionPolicy(ai_assist=True),
                  max_steps=1)
    db.refresh(session)
    # Must not be completed, must not have filled junk, must pause for user.
    assert session.state == "awaiting_user"
    assert driver.fills == []
    assert result["next_step"]["reason"] == "ai_unavailable"
