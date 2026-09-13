"""v2.1 — AI as a hard dependency with honest pause/resume.

The contract under test (see CHANGELOG 2.1.0):

1. A *synchronous* AI call that hits a **transient outage** (timeout /
   unreachable / 429 / 5xx / breaker / budget) returns a dedicated **pausable
   503** — ``code=ai_unavailable``, ``status="ai_paused"``,
   ``state="transient_outage"``, a non-blank actionable ``detail`` and a
   ``retry_after_hint`` — never a generic 500, and never a guessed result.
2. A **needs-action** failure (no key / invalid key / quota) returns
   ``status="ai_blocked"`` / ``state="blocked_needs_action"`` with exactly ONE
   wire attempt — retrying a blocked account is pointless, so there is no
   retry storm.
3. **Queued work** that hits a transient outage mid-run is *paused* (re-queued
   with backoff, never marked failed or completed, the pause does not consume
   an attempt — and since v2.2.2 neither does the claim that follows, because
   ``attempts`` counts handler failures only). When the provider is back, the
   watchdog (or the one-click ``POST /api/settings/ai/resume``) drains it and
   the worker completes it **without duplicates**.
4. **Nothing is persisted before the AI call succeeds** — an upload interrupted
   by an outage leaves no half-built resume/profile behind, so the retry is
   safe.

Every test drives the REAL gateway against the local scripted
OpenAI-compatible provider (the hermetic ``real_ai`` convention from
``test_ai_timeout_fixes.py``) — no stubs on the wire path, no outbound
network.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

import pytest

# Top-level `conftest` (the module pytest loads for the fixtures) — importing
# `tests.conftest` would be a *second* module instance with its own state.
from conftest import pdf_bytes


# --------------------------------------------------------------------------- #
# Isolation: breaker + ping-cache state is process-global; keep it clean.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_ai_state():
    import app.services.ai_client as ai_client

    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()
    yield
    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()


def _behavior() -> Dict[str, Any]:
    # NOTE: import top-level `conftest` (the module pytest loads for the
    # fixtures) — `tests.conftest` would be a *second* module instance with a
    # different ScriptedAIHandler class and its own behavior dict.
    import conftest

    return conftest.ScriptedAIHandler.behavior


def _wire() -> list:
    return _behavior()["requests"]


def _set_outage(status: int = 503) -> None:
    """The scripted provider goes down from the very next request on."""
    b = _behavior()
    b["status"] = status
    b["fail_after"] = b.get("good", 0)  # everything from now on fails



def _tag_job(db, user_id=None):
    from app.models.models import PipelineJob

    query = db.query(PipelineJob).filter(PipelineJob.pipeline == "ai")
    if user_id is not None:
        query = query.filter(PipelineJob.user_id == user_id)
    for item in query.all():
        if (item.payload or {}).get("task") == "tag_resume":
            return item
    return None

def _set_recovery() -> None:
    """The scripted provider comes back."""
    _behavior()["fail_after"] = None


# --------------------------------------------------------------------------- #
# 1. In-flight synchronous HTTP → dedicated pausable 503, nothing persisted
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_upload_outage_is_pausable_and_persists_nothing(
        client, auth, db, provider_owner, monkeypatch):
    """A 5xx mid-upload → pausable 503 with full diagnosis; no partial rows."""
    from conftest import pdf_bytes

    import app.services.ai_client as ai_client
    from app.models.models import Profile, Resume

    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    _set_outage(503)

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "ai_unavailable", body
    assert body["reason"] == "provider_error", body
    assert body["state"] == "transient_outage", body
    assert body["status"] == "ai_paused", body
    assert body["pausable"] is True, body
    assert body["retry_after_hint"] is not None, body
    assert body["workflow"] == "parse", body
    assert body["detail"].strip(), "detail must not be blank"
    assert "503" in body["detail"], body["detail"]

    # Exactly one wire attempt (max_retries=1): the pausable outcome must be
    # purely additive on top of the v2.0.5 accounting.
    parse_calls = [r for r in _wire()
                   if any("extract a complete, structured profile" in str(m.get("content") or "").lower()
                          for m in r.get("messages") or [])]
    assert len(parse_calls) == 1, f"expected 1 wire attempt, saw {len(parse_calls)}"

    # Nothing persisted before the AI call succeeded — the retry is safe.
    assert db.query(Resume).count() == 0, "no Resume row may survive an interrupted upload"
    assert db.query(Profile).count() == 0, "no Profile row may survive an interrupted upload"

    # The failed attempt is still ledgered (attempts + honesty), not hidden.
    from app.models.models import AICreditLedger

    row = (db.query(AICreditLedger).filter(AICreditLedger.workflow == "parse")
           .order_by(AICreditLedger.id.desc()).first())
    assert row is not None and not row.success
    assert row.meta.get("attempts") == 1


@pytest.mark.real_ai
def test_classify_outage_returns_pausable_503_never_a_guessed_size(
        client, auth, db, provider_owner, monkeypatch):
    """The interactive classify endpoint is the AI verdict — an outage is a
    pausable 503, not a deterministic guess dressed up as a size."""
    import app.services.ai_client as ai_client

    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    _set_outage(503)

    response = client.post(
        "/api/classify/company?company=Stealth+AI+Startup&jd=seed+stage+3+people",
        headers=auth,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "ai_unavailable"
    assert body["status"] == "ai_paused"
    assert body["state"] == "transient_outage"
    assert body["detail"].strip()
    assert "size" not in body, "an outage must never be answered with a size"


# --------------------------------------------------------------------------- #
# 2. Blocked (invalid key) → ai_blocked, exactly one wire attempt
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_invalid_key_is_blocked_with_no_retry_storm(client, auth, db, provider_owner):
    """A provider that rejects the key (401) is blocked_needs_action: no
    retries (a bad key will not fix itself), and the UI is pointed at
    Settings → AI API."""
    from conftest import pdf_bytes

    import app.services.ai_client as ai_client
    from app.models.models import Profile, Resume

    # A 401 on every request — the scripted provider "rejects the key".
    _behavior()["status"] = 401
    _behavior()["fail_after"] = 0

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["reason"] == "invalid_api_key", body
    assert body["state"] == "blocked_needs_action", body
    assert body["status"] == "ai_blocked", body
    assert body["pausable"] is False, body
    assert "Settings" in body["fix"], "the fix must point at Settings → AI API"

    # No retry storm: a 401 must produce exactly ONE wire attempt.
    assert len(_wire()) == 1, f"a blocked key must not be retried, saw {len(_wire())} wire request(s)"
    assert db.query(Resume).count() == 0
    assert db.query(Profile).count() == 0


@pytest.mark.real_ai
def test_settings_status_reports_blocked_state(client, auth, db, provider_owner):
    """The polled availability signal flips to blocked_needs_action on a bad
    key — this is what the UI banner renders."""
    import app.services.ai_client as ai_client

    _behavior()["status"] = 401
    _behavior()["fail_after"] = 0

    status = client.get("/api/settings/ai/status", headers=auth)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["online"] is False
    assert body["state"] == "blocked_needs_action", body
    assert body["reason"] == "invalid_api_key", body
    assert body["paused_count"] == 0


# --------------------------------------------------------------------------- #
# 3. Queued work: paused (not failed), recovered, completed without duplicates
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_queued_job_paused_on_outage_then_recovers_without_duplicates(
        client, auth, db, provider_owner, monkeypatch):
    """Mid-run outage on a queued job: the item is *paused* (never failed or
    completed, the pause consumes no attempt). Recovery + drain completes it
    exactly once — no duplicate side effects."""
    from conftest import pdf_bytes

    from app.models.models import AICreditLedger, PipelineJob, Resume
    from app.services.job_queue import drain_paused, paused_count
    from app.worker import Worker

    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)

    # 1) Upload while the provider is healthy — the real wire path (parse,
    #    context, persona) and the tag_resume job the upload enqueues.
    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    resume = db.query(Resume).filter(Resume.type == "master").first()
    assert resume is not None
    item = _tag_job(db, resume.user_id)
    assert item is not None, "the upload must enqueue the tagging job"
    assert item.status == "queued"

    # 2) The provider dies mid-run — the worker executes the job and pauses it.
    _set_outage(503)
    worker = Worker(pipelines=["ai"])
    await worker._run_item(item.id, "ai")

    db.refresh(item)
    assert item.status == "paused", f"an AI outage must pause, not fail: {item.status} ({item.error})"
    assert item.finished_at is None, "paused work is never 'completed'"
    # v2.2.2: attempts counts handler *failures*. The claim took a lease and
    # the pause parked the item — neither is a failure, so the budget is intact
    # (was: == 1, when the claim itself incremented the counter).
    assert item.attempts == 0, "neither the claim nor the pause may consume a failure attempt"
    assert (item.payload or {}).get("paused_count") == 1
    assert paused_count(db, user_id=resume.user_id) == 1
    db.refresh(resume)
    assert resume.tags == ["master"], "no AI tags until the call actually succeeds"
    # The paused attempt was ledgered as a failure, honestly.
    failed_ledger = (db.query(AICreditLedger).filter(AICreditLedger.workflow == "tagging",
                                                     AICreditLedger.success.is_(False)).count())
    assert failed_ledger == 1

    # 3) The provider recovers; the one-click resume drains the paused work.
    _set_recovery()
    resumed = drain_paused(db, user_id=resume.user_id, force=True)
    assert resumed == 1
    db.refresh(item)
    assert item.status == "queued"

    # 4) The worker picks it up and completes it — exactly once.
    await worker._run_item(item.id, "ai")
    db.refresh(item)
    assert item.status == "done", item.error
    assert (item.payload or {}).get("paused_count") == 1, "no second pause may happen"
    db.refresh(resume)
    assert resume.tags and resume.tags != ["master"], "recovery must complete the tagging"

    # No duplicates: one successful tagging ledger row, one resume, one job.
    success_rows = db.query(AICreditLedger).filter(
        AICreditLedger.workflow == "tagging", AICreditLedger.success.is_(True)).all()
    assert len(success_rows) == 1, "the recovered run must bill exactly once"
    assert db.query(Resume).filter(Resume.type == "master").count() == 1
    assert len([i for i in db.query(PipelineJob).all()
                if (i.payload or {}).get("task") == "tag_resume"]) == 1


@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_watchdog_drains_paused_work_only_when_online(
        client, auth, db, provider_owner, monkeypatch):
    """The watchdog re-probes on schedule: while the provider is down the
    paused work stays paused; the moment the probe is green it drains."""
    from conftest import pdf_bytes

    import app.services.ai_watchdog as watchdog
    from app.models.models import PipelineJob, Resume
    from app.services.job_queue import paused_count
    from app.worker import Worker

    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    resume = db.query(Resume).filter(Resume.type == "master").first()
    item = _tag_job(db, resume.user_id)

    _set_outage(503)
    await Worker(pipelines=["ai"])._run_item(item.id, "ai")
    db.refresh(item)
    assert item.status == "paused"
    # The pause backoff (scheduled_at in the future) has not elapsed yet —
    # rewind it to simulate the backoff window passing.
    item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()

    # Still down → the probe is not green → nothing drains.
    assert await watchdog.watchdog_cycle() == 0
    db.refresh(item)
    assert item.status == "paused", "the watchdog must not drain during an outage"

    # Provider back → the probe is green → the paused work drains.
    _set_recovery()
    import app.services.ai_client as ai_client

    ai_client._PING_CACHE.clear()  # the 60s probe cache must not mask recovery
    assert await watchdog.watchdog_cycle() == 1
    db.refresh(item)
    assert item.status == "queued"
    assert paused_count(db, user_id=resume.user_id) == 0


# --------------------------------------------------------------------------- #
# 4. The status endpoint + one-click resume
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_status_exposes_state_and_resume_endpoint(client, auth, db, provider_owner, monkeypatch):
    """GET /settings/ai/status carries the three-state signal + paused count;
    POST /settings/ai/resume force-drains the user's paused work."""
    import app.services.ai_client as ai_client
    from app.models.models import PipelineJob, Resume
    from app.services.job_queue import enqueue, paused_count

    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    resume = db.query(Resume).filter(Resume.type == "master").first()
    user = db.query(PipelineJob).filter(PipelineJob.user_id == resume.user_id).first().user_id

    # Healthy → online, nothing paused.
    status = client.get("/api/settings/ai/status", headers=auth)
    assert status.status_code == 200
    body = status.json()
    assert body["online"] is True
    assert body["state"] == "online", body
    assert body["paused_count"] == 0

    # Park one item as paused (the state machine the worker uses on outage).
    item = enqueue(db, user_id=user, pipeline="ai",
                   payload={"task": "tag_resume", "resume_id": resume.id},
                   dedupe_key="tag_resume:status-test")
    item.status = "paused"
    item.scheduled_at = datetime.utcnow() + timedelta(seconds=60)
    db.commit()

    # Outage → transient_outage, paused_count visible, retry hint present.
    _set_outage(503)
    ai_client._PING_CACHE.clear()  # the 60s probe cache must not mask the outage
    body = client.get("/api/settings/ai/status", headers=auth).json()
    assert body["state"] == "transient_outage", body
    assert body["reason"] == "provider_error", body
    assert body["paused_count"] == 1
    assert body["retry_after_hint"] is not None

    # One-click resume: force-drain now, and report the (still down) state so
    # the UI can explain what will happen to the re-queued work.
    response = client.post("/api/settings/ai/resume", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["resumed"] == 1
    assert body["state"] == "transient_outage", body
    assert paused_count(db, user_id=user) == 0
    db.refresh(item)
    assert item.status == "queued"

    # Recovery: the state endpoint goes green.
    _set_recovery()
    ai_client._PING_CACHE.clear()
    body = client.get("/api/settings/ai/status", headers=auth).json()
    assert body["state"] == "online", body
