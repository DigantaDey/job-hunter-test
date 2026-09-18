"""
Onboarding endpoints — the resumable, user-facing journey.

Every endpoint returns the *whole* status document (see
``onboarding.status_document``), so a client never merges a partial answer
into its own idea of the state. Every query is tenant-scoped by the
authenticated user: a foreign session id is a 404, never another user's data.

The upload endpoint deliberately does **no AI work** — it validates the file,
stores the original, records the document + extraction attempt rows, enqueues
the background job and answers. The model runs in the ``extraction`` pipeline
worker; the client polls :route:`GET /api/onboarding/status`, which reconciles
the session against stored facts on every read (that reconciliation is what
makes a browser refresh, a worker restart or a deploy invisible to the user).
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.config import settings
from app.core.entitlements import enforce
from app.core.logging import get_logger
from app.services import onboarding as onboarding_service
from app.services.onboarding import (
    get_or_create_session,
    get_session_by_id,
    reconcile_session,
    register_upload,
    retry_blocked_extraction,
    status_document,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])
log = get_logger("app.onboarding_api")

# Reuse the exact validation/storage helpers the legacy upload uses, so both
# flows accept and store documents identically.
from app.api.routers.resumes import _store_upload, _validate_upload  # noqa: E402


@router.post("/session")
def create_session(request: Request, user: CurrentUser, db: DbSession):
    """Create (or return — idempotent) the user's durable onboarding session.

    The id is the durable handle the SPA stores: refreshing the page, closing
    the tab or coming back tomorrow all resolve to the same session and the
    same state, because the state is derived server-side from stored facts.
    """
    session = get_or_create_session(db, user, request_id=request.headers.get("x-request-id", ""))
    return status_document(db, user, session)


@router.get("/status")
def get_status(user: CurrentUser, db: DbSession):
    """The reconciled status document — the only thing the SPA polls.

    Reconciliation runs on every read: if a worker died, the queue row went
    terminal without its bookkeeping, or an enqueue was lost, this read repairs
    the session (retryable blocked state / re-enqueue) before answering.
    """
    session = get_or_create_session(db, user)
    reconcile_session(db, user, session)
    return status_document(db, user, session)


@router.get("/sessions/{session_id}")
def get_session_by_id_route(session_id: int, user: CurrentUser, db: DbSession):
    """Fetch one session by id — cross-tenant ids are 404s, never data."""
    session = get_session_by_id(db, user.id, session_id)
    if not session:
        raise HTTPException(404, "Onboarding session not found")
    reconcile_session(db, user, session)
    return status_document(db, user, session)


@router.post("/resume")
async def upload_resume(request: Request, user: CurrentUser, db: DbSession,
                        file: UploadFile = File(...)):
    """Attach a master resume to the session and queue background extraction.

    Contract T4. Returns immediately with the session in ``resume_processing``;
    the AI extraction runs in the queue and the session moves to
    ``profile_review_required`` (or ``extraction_blocked``) without any further
    user action.

    * Quota is enforced here, at request time (``resume_parses_per_month`` /
      ``resumes_max``) — the same gates the legacy upload applies. The counter
      itself is charged once, on the extraction's first success.
    * Identical bytes re-uploaded are a no-op: the same document row is
      returned and the session re-attaches to its extraction state.
    * A *different* document replaces the current master: the previous one is
      archived (never deleted) and its in-flight extraction is superseded.
    """
    enforce(db, user.id, "resume_parses_per_month")
    enforce(db, user.id, "resumes_max")

    session = get_or_create_session(
        db, user, request_id=request.headers.get("x-request-id", ""))

    head = await file.read(8)
    await file.seek(0)
    try:
        extension = _validate_upload(file, head)
    except HTTPException as exc:
        # Typed blocked codes (contract 04 §7) — the SPA can offer the exact fix.
        code = "unsupported_media_type" if exc.status_code == 415 else "validation_error"
        detail = exc.detail if isinstance(exc.detail, str) else code
        raise HTTPException(exc.status_code, {"code": code, "message": detail}) from exc

    path, safe_name = _store_upload(file, extension)
    content_type = file.content_type or ("application/pdf" if extension == ".pdf" else
                                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    size_bytes = 0
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
            size_bytes += len(chunk)

    size_mb = size_bytes / (1024 * 1024)
    if size_mb > settings.max_upload_mb:
        try:
            import os

            os.remove(path)
        except OSError:  # pragma: no cover - filesystem dependent
            pass
        raise HTTPException(413, {"code": "payload_too_large",
                                  "message": f"File is {size_mb:.1f}MB — the limit is {settings.max_upload_mb}MB"})

    document, created = register_upload(
        db, user, session,
        filepath=path, filename=safe_name, content_type=content_type,
        sha256_hex=sha.hexdigest(), size_bytes=size_bytes,
    )

    if created:
        audit.audit(db, "resume.uploaded", user=user, target=safe_name, request=request,
                    detail={"document_id": document.id, "session_id": session.id,
                            "size_bytes": size_bytes, "flow": "onboarding"})
        onboarding_service.start_extraction(db, user, session, document, trigger="user")
    else:
        # Same bytes again: re-attach to the document's current extraction —
        # an in-flight run simply continues; a finished one is already reflected.
        onboarding_service.reattach_in_flight(db, session, document)

    reconcile_session(db, user, session)
    return status_document(db, user, session)


@router.post("/retry")
def retry(request: Request, user: CurrentUser, db: DbSession):
    """Re-run the blocked extraction (contract T8).

    Only a retryable blocked state qualifies (``ai_unavailable``, worker
    crashes); guardrail rejections and unreadable documents are refused with
    ``409`` and ``{"code": "not_retryable"}`` — those need a different upload,
    not another identical attempt. Returns the status document either way.
    """
    session = get_or_create_session(db, user)
    reconcile_session(db, user, session)
    ok, reason = retry_blocked_extraction(db, user, session)
    if not ok:
        raise HTTPException(409, {"code": reason, "message": _retry_refusal(reason, session)})
    return status_document(db, user, session)


def _retry_refusal(reason: str, session) -> str:
    if reason == "not_retryable":
        return ("This failure needs a different document or configuration — "
                "retrying the same upload will not change the outcome.")
    if reason == "not_blocked":
        return "Nothing to retry — no extraction is currently blocked."
    if reason == "document_missing":
        return "The uploaded document is gone — upload it again."
    return "Retry is not available right now."
