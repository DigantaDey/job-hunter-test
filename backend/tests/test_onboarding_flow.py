"""
Resumable onboarding — the backend foundation contract.

Covers the acceptance criteria end to end against the real app + durable queue:

* session creation is durable and idempotent (refresh = same session + state);
* the upload request never blocks on AI — extraction runs in the queue;
* a worker failure produces a *retryable* blocked state, and retry recovers;
* a successful extraction lands in a reviewable state with one Profile row;
* duplicate job execution and duplicate uploads are idempotent (no duplicate
  profile/resume/queue rows, quota charged once);
* cross-tenant session/document access is a 404, never data;
* replacing a resume archives the old document and supersedes its extraction;
* stored errors carry no provider secrets; the original file is preserved;
* a lost queue row (worker-restart window) self-heals on the next status read.
"""
from __future__ import annotations

import os
from typing import Any, Dict

import pytest

from app.models.models import (
    Notification,
    OnboardingEvent,
    OnboardingSession,
    PipelineJob,
    Profile,
    Resume,
    ResumeDocument,
    ResumeExtraction,
    User,
)
from app.services.ai_guardrails import AIUnavailableError
from tests.conftest import _stub_profile, pdf_bytes, run_queue_item


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def upload(client, headers, name: str = "resume.pdf", content: bytes | None = None) -> Dict[str, Any]:
    response = client.post(
        "/api/onboarding/resume",
        files={"file": (name, content if content is not None else pdf_bytes(), "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def status(client, headers) -> Dict[str, Any]:
    response = client.get("/api/onboarding/status", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def counts(db) -> Dict[str, int]:
    return {
        "profiles": db.query(Profile).count(),
        "resumes": db.query(Resume).filter(Resume.type == "master").count(),
        "documents": db.query(ResumeDocument).count(),
        "extractions": db.query(ResumeExtraction).count(),
        "jobs": db.query(PipelineJob).filter(PipelineJob.pipeline == "extraction").count(),
        "reviews": db.query(Notification).filter(Notification.kind == "profile_review_required").count(),
    }


# --------------------------------------------------------------------------- #
# Session creation
# --------------------------------------------------------------------------- #
def test_session_creation_is_durable_and_idempotent(client, auth):
    first = client.post("/api/onboarding/session", headers=auth)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["session_id"]
    assert body["state"] == "awaiting_resume"

    # A second click, a page refresh, a second device — same session, same state.
    again = client.post("/api/onboarding/session", headers=auth)
    assert again.json()["session_id"] == body["session_id"]
    fetched = status(client, auth)
    assert fetched["session_id"] == body["session_id"]
    assert fetched["state"] == "awaiting_resume"
    assert fetched["resume_document"] is None


# --------------------------------------------------------------------------- #
# Success path: non-blocking upload → background extraction → reviewable
# --------------------------------------------------------------------------- #
def test_upload_is_non_blocking_then_extraction_completes(client, auth, db):
    doc_status = upload(client, auth)

    # The request returned with work still in flight — never a profile.
    assert doc_status["state"] == "resume_processing"
    assert doc_status["extraction"]["state"] in ("pending", "running")
    assert doc_status["live_work"]["pipeline_job_id"]
    assert doc_status["resume_document"]["filename"] == "resume.pdf"
    assert doc_status["blocked"]["code"] is None

    # The original document is preserved on disk and recorded before the AI ran.
    job_id = doc_status["live_work"]["pipeline_job_id"]
    extraction_id = doc_status["extraction"]["id"]
    document = db.query(ResumeDocument).filter_by(id=doc_status["resume_document"]["id"]).one()
    assert os.path.exists(document.filepath)
    assert document.state == "extracting"
    db.expire_all()

    run_queue_item(job_id, "extraction")

    final = status(client, auth)
    assert final["session_id"] == doc_status["session_id"]
    assert final["state"] == "profile_review_required"
    assert final["extraction"]["id"] == extraction_id
    assert final["extraction"]["state"] == "succeeded"
    assert final["extraction"]["extractor_version"]
    assert final["blocked"]["code"] is None
    assert final["progress"]["percent"] == 100

    # One profile, one master resume, exactly one extraction attempt.
    profile = db.query(Profile).one()
    assert profile.source_extraction_id == extraction_id
    assert profile.data["email"] == "test.candidate@example.com"
    resume = db.query(Resume).filter_by(type="master").one()
    assert resume.status == "approved"
    assert profile.master_resume_id == resume.id

    # The monthly parse counter was charged by the worker (same entitlement the
    # legacy upload used) — and the extraction carries its own AI accounting.
    assert _parse_usage_count(db) == 1

    # The user is told — one review notification, not a duplicate storm.
    notifications = db.query(Notification).filter_by(kind="profile_review_required").all()
    assert len(notifications) == 1
    assert notifications[0].meta["extraction_id"] == extraction_id

    # The timeline is append-only and ordered by sequence, not by clock.
    events = (db.query(OnboardingEvent)
              .filter_by(session_id=doc_status["session_id"])
              .order_by(OnboardingEvent.sequence).all())
    types = [event.event_type for event in events]
    assert "resume.uploaded" in types and "extraction.started" in types
    assert "extraction.succeeded" in types and "onboarding.state_changed" in types
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)

    # The legacy product keeps working off the same artifacts.
    listing = client.get("/api/resumes", headers=auth)
    assert listing.status_code == 200, listing.text
    assert any(row["id"] == resume.id for row in listing.json())


# --------------------------------------------------------------------------- #
# Refresh / resume behavior
# --------------------------------------------------------------------------- #
def test_refresh_returns_same_session_and_state(client, auth, db):
    before = upload(client, auth)
    session_id = before["session_id"]

    # Refresh before the worker ran: identical document, same in-flight state.
    refreshed = status(client, auth)
    assert refreshed["session_id"] == session_id
    assert refreshed["state"] == "resume_processing"
    assert refreshed["extraction"]["id"] == before["extraction"]["id"]
    assert refreshed["resume_document"]["sha256"] == before["resume_document"]["sha256"]

    # Fetching by the durable id (bookmark/share) resolves to the same session.
    by_id = client.get(f"/api/onboarding/sessions/{session_id}", headers=auth)
    assert by_id.status_code == 200
    assert by_id.json()["session_id"] == session_id

    # The work survives: run it after the "refreshes" — state advances once.
    run_queue_item(before["live_work"]["pipeline_job_id"], "extraction")
    after = status(client, auth)
    assert after["session_id"] == session_id
    assert after["state"] == "profile_review_required"


def test_status_read_repairs_dead_worker_crash(client, auth, db):
    """Worker died hard (queue row dead, no bookkeeping) → retryable blocked."""
    before = upload(client, auth)
    job = db.query(PipelineJob).filter_by(id=before["live_work"]["pipeline_job_id"]).one()
    job.status = "dead"
    job.error = "worker OOM-killed"
    db.commit()

    repaired = status(client, auth)
    assert repaired["state"] == "extraction_blocked"
    assert repaired["blocked"]["retryable"] is True
    assert repaired["blocked"]["code"]
    assert repaired["extraction"]["state"] == "failed"
    # The user is told the flow needs attention.
    alerts = db.query(Notification).filter_by(kind="onboarding_step_required").count()
    assert alerts == 1


def test_lost_queue_row_self_heals_on_read(client, auth, db):
    """Enqueue lost in a deploy window → the next status read re-enqueues."""
    before = upload(client, auth)
    db.query(PipelineJob).filter_by(id=before["live_work"]["pipeline_job_id"]).delete()
    db.commit()

    healed = status(client, auth)
    assert healed["state"] == "resume_processing"
    assert healed["live_work"]["pipeline_job_id"], "a queue row must exist again"
    extraction = db.query(ResumeExtraction).filter_by(id=healed["extraction"]["id"]).one()
    assert extraction.trigger == "recovery"
    assert extraction.pipeline_job_id == healed["live_work"]["pipeline_job_id"]

    run_queue_item(healed["live_work"]["pipeline_job_id"], "extraction")
    assert status(client, auth)["state"] == "profile_review_required"


# --------------------------------------------------------------------------- #
# Retryable failure handling
# --------------------------------------------------------------------------- #
def test_transient_ai_outage_pauses_without_blocking_session(client, auth, db, monkeypatch):
    from app.services import resume_parser

    async def flaky(*args, **kwargs):
        raise AIUnavailableError("timeout", workflow="parse", state="transient_outage",
                                 detail="provider timed out")

    monkeypatch.setattr(resume_parser, "ai_extract_profile", flaky)
    before = upload(client, auth)
    run_queue_item(before["live_work"]["pipeline_job_id"], "extraction")

    row = db.query(PipelineJob).filter_by(id=before["live_work"]["pipeline_job_id"]).one()
    assert row.status == "paused"  # the queue owns the retry — no user action needed

    # A paused extraction is still *in flight*: the session says wait, not fix.
    current = status(client, auth)
    assert current["state"] == "resume_processing"
    assert current["blocked"]["code"] is None
    assert current["extraction"]["error_code"] == "ai_unavailable"


def test_hard_failure_then_retry_recovers(client, auth, db, monkeypatch):
    from app.services import resume_parser

    async def broken(*args, **kwargs):
        raise RuntimeError("extraction crashed midway")

    monkeypatch.setattr(resume_parser, "ai_extract_profile", broken)
    before = upload(client, auth)

    # One failure is enough when the failure budget is spent.
    job = db.query(PipelineJob).filter_by(id=before["live_work"]["pipeline_job_id"]).one()
    job.max_attempts = 1
    db.commit()
    run_queue_item(job.id, "extraction")
    db.expire_all()  # the worker ran in its own session — re-read the row
    assert db.query(PipelineJob).filter_by(id=job.id).one().status == "dead"

    blocked = status(client, auth)
    assert blocked["state"] == "extraction_blocked"
    assert blocked["blocked"]["retryable"] is True
    assert blocked["next_action"]["label"] == "Retry extraction"

    # The AI comes back; the user clicks retry; the flow completes.
    async def working(*args, **kwargs):
        return dict(_stub_profile()), {"source": "stub", "model": "scripted-model"}

    monkeypatch.setattr(resume_parser, "ai_extract_profile", working)
    retry_response = client.post("/api/onboarding/retry", headers=auth)
    assert retry_response.status_code == 200, retry_response.text
    retried = retry_response.json()
    assert retried["state"] == "resume_processing"
    assert retried["extraction"]["attempt"] == 2
    assert retried["extraction"]["trigger"] == "retry"

    run_queue_item(retried["extraction"]["pipeline_job_id"], "extraction")
    done = status(client, auth)
    assert done["state"] == "profile_review_required"


def test_retry_refused_when_not_retryable(client, auth, db, monkeypatch):
    """Guardrail/no-text rejections need a different document, not a retry."""
    from app.services import resume_parser

    async def rejected(*args, **kwargs):
        from app.services.ai_guardrails import GuardrailError

        raise GuardrailError("parse", [{"code": "grounding", "message": "invented employer"}])

    monkeypatch.setattr(resume_parser, "ai_extract_profile", rejected)
    before = upload(client, auth)
    run_queue_item(before["live_work"]["pipeline_job_id"], "extraction")

    blocked = status(client, auth)
    assert blocked["state"] == "extraction_blocked"
    assert blocked["blocked"]["retryable"] is False
    assert blocked["blocked"]["code"] == "guardrail_failed"
    assert blocked["next_action"]["label"] == "Upload a different resume"

    refusal = client.post("/api/onboarding/retry", headers=auth)
    assert refusal.status_code == 409
    assert refusal.json()["detail"]["code"] == "not_retryable"


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_duplicate_job_execution_is_idempotent(client, auth, db):
    before = upload(client, auth)
    job_id = before["live_work"]["pipeline_job_id"]
    run_queue_item(job_id, "extraction")

    first = counts(db)
    assert first["profiles"] == 1 and first["resumes"] == 1
    assert _parse_usage_count(db) == 1

    # Run the *same* queue item again — a watchdog replay or an operator error.
    run_queue_item(job_id, "extraction")

    second = counts(db)
    assert second == first, "duplicate execution must not create any row"
    assert _parse_usage_count(db) == 1, "quota must be charged once"
    assert status(client, auth)["state"] == "profile_review_required"


def _parse_usage_count(db) -> int:
    from app.models.models import UsageCounter

    row = db.query(UsageCounter).filter_by(capability="resume_parses_per_month").first()
    return int(row.count or 0) if row else 0


def test_duplicate_upload_of_same_bytes_is_a_noop(client, auth, db):
    content = pdf_bytes()
    first = upload(client, auth, content=content)
    first_document_id = first["resume_document"]["id"]
    run_queue_item(first["live_work"]["pipeline_job_id"], "extraction")
    assert status(client, auth)["state"] == "profile_review_required"

    again = upload(client, auth, content=content)
    assert again["resume_document"]["id"] == first_document_id
    assert again["state"] == "profile_review_required"
    assert again["extraction"]["attempt"] == 1

    # Re-attaching to finished work enqueues nothing new.
    jobs = db.query(PipelineJob).filter_by(pipeline="extraction").count()
    assert jobs == 1
    assert db.query(ResumeExtraction).count() == 1
    assert counts(db)["profiles"] == 1


# --------------------------------------------------------------------------- #
# Replacement behavior
# --------------------------------------------------------------------------- #
def test_replacement_archives_old_document_and_supersedes_extraction(client, auth, db):
    first = upload(client, auth, name="old.pdf")
    run_queue_item(first["live_work"]["pipeline_job_id"], "extraction")
    assert status(client, auth)["state"] == "profile_review_required"
    old_document_id = first["resume_document"]["id"]

    replacement = upload(client, auth, name="new.pdf", content=pdf_bytes() + b"\nupdated")
    assert replacement["resume_document"]["id"] != old_document_id
    assert replacement["state"] == "resume_processing"

    old_doc = db.query(ResumeDocument).filter_by(id=old_document_id).one()
    assert old_doc.state == "archived"
    assert old_doc.archived_at is not None
    assert os.path.exists(old_doc.filepath), "the original stays on disk"
    old_extractions = (db.query(ResumeExtraction)
                       .filter_by(resume_document_id=old_document_id).all())
    # A finished attempt stays as history; only in-flight work is superseded
    # (a replacement mid-flight is covered by the sanitizer/supersede guard).
    assert all(extraction.state in ("succeeded", "superseded")
               for extraction in old_extractions)

    run_queue_item(replacement["live_work"]["pipeline_job_id"], "extraction")
    final = status(client, auth)
    assert final["state"] == "profile_review_required"
    assert final["resume_document"]["id"] == replacement["resume_document"]["id"]

    # Exactly one active profile, owned by the newest extraction.
    profiles = db.query(Profile).all()
    assert len(profiles) == 1
    assert profiles[0].source_extraction_id == final["extraction"]["id"]
    masters = db.query(Resume).filter_by(type="master").order_by(Resume.created_at).all()
    assert [resume.status for resume in masters] == ["archived", "approved"]


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #
def test_cross_tenant_access_is_404_never_data(client, auth, member_auth, db):
    mine = upload(client, auth)
    run_queue_item(mine["live_work"]["pipeline_job_id"], "extraction")

    foreign = client.get(f"/api/onboarding/sessions/{mine['session_id']}", headers=member_auth)
    assert foreign.status_code == 404

    theirs = status(client, member_auth)
    assert theirs["session_id"] != mine["session_id"]
    assert theirs["state"] == "awaiting_resume"
    assert theirs["resume_document"] is None
    assert theirs["extraction"] is None

    # A member cannot drive another user's retry either.
    refusal = client.post("/api/onboarding/retry", headers=member_auth)
    assert refusal.status_code == 409  # their own session — simply not blocked

    anonymous = client.get("/api/onboarding/status")
    assert anonymous.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Safe error storage & preserved originals
# --------------------------------------------------------------------------- #
def test_stored_errors_never_leak_provider_secrets(client, auth, db, monkeypatch):
    from app.services import resume_parser

    async def leaky(*args, **kwargs):
        raise RuntimeError(
            "provider 401 for api_key=sk-live-abcdef1234567890 bearer eyJhbGciOiJIUzI1NIfakefakefake token=zzz-secret")

    monkeypatch.setattr(resume_parser, "ai_extract_profile", leaky)
    before = upload(client, auth)
    job = db.query(PipelineJob).filter_by(id=before["live_work"]["pipeline_job_id"]).one()
    job.max_attempts = 1
    db.commit()
    run_queue_item(job.id, "extraction")

    blocked = status(client, auth)
    surfaced = {
        blocked["blocked"]["message"] or "",
        blocked["extraction"]["error_message"] or "",
    }
    for text in surfaced:
        assert "sk-live-abcdef1234567890" not in text
        assert "zzz-secret" not in text
        assert "eyJhbGciOiJIUzI1NIfakefakefake" not in text
    assert "api_key" in " ".join(surfaced) or "***" in " ".join(surfaced)

    document = db.query(ResumeDocument).filter_by(id=before["resume_document"]["id"]).one()
    assert document.error_message is None or "sk-live" not in document.error_message
    assert os.path.exists(document.filepath), "failure must not cost the user their document"


def test_sanitizer_masks_key_shapes():
    from app.services.onboarding import safe_error_message

    cleaned = safe_error_message("line1\napi_key: sk-proj-ABCDEF1234567890\tAuthorization: Bearer abc.def.ghi")
    assert "sk-proj-ABCDEF1234567890" not in cleaned
    assert "abc.def.ghi" not in cleaned
    assert "\n" not in cleaned and "\t" not in cleaned
