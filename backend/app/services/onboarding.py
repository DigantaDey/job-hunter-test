"""
Resumable onboarding: session state machine, durable uploads and the
background extraction job.

Why this exists
---------------
The legacy ``POST /api/resume/upload`` did everything inside one long HTTP
request — validate, store, extract text, call the model, build the search
context — with progress in an in-process dict. A refresh, a deploy, a worker
crash or a slow provider lost the work and left the user staring at an error.
This module moves the *AI extraction* half into the durable queue and makes the
journey state a function of stored facts:

* :class:`app.models.models.OnboardingSession` — one row per user; ``state`` is
  a persisted projection that :func:`reconcile_session` recomputes from the
  document, the extraction attempt and the queue row on every read.
* :class:`app.models.models.ResumeDocument` — the original upload, preserved on
  disk and content-hashed; re-uploading identical bytes is a no-op.
* :class:`app.models.models.ResumeExtraction` — one append-only row per
  attempt, created *before* the model is called, versioned by
  :data:`EXTRACTOR_VERSION`.

Scope (backend foundation, contracts 03/04): the *resume* gate only —
``awaiting_resume → resume_processing → (extraction_blocked |
profile_review_required)``. Gates beyond it (field-level profile review,
preferences, discovery, automation) are later deliverables and deliberately not
claimed here: ``completed_at`` stays ``NULL`` and the derived state stops at
``profile_review_required`` ("your profile is ready to review").

Safety properties (all tested in ``tests/test_onboarding_flow.py``):

* every query is tenant-scoped — a foreign session id is a 404, never data;
* the HTTP request never waits on the model — the handler runs in the
  ``extraction`` pipeline of the durable queue;
* errors are stored via :func:`safe_error_message`, which scrubs API keys,
  bearer tokens and secrets before anything is persisted;
* running the same queue item twice, or two jobs for the same document, is
  idempotent: the profile is upserted on ``Profile.source_extraction_id`` and a
  superseded attempt never writes.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import (
    OnboardingEvent,
    OnboardingSession,
    PipelineJob,
    Profile,
    Resume,
    ResumeDocument,
    ResumeExtraction,
    User,
)
from app.services.resume_service import render_profile_text

log = get_logger("app.onboarding")

# --------------------------------------------------------------------------- #
# Vocabulary (mirrors app.contracts.vocabulary — see the drift test there)
# --------------------------------------------------------------------------- #
#: Queue pipeline + task name for the background extraction.
EXTRACTION_PIPELINE = "extraction"
EXTRACTION_TASK = "extract_resume_profile"

#: The extractor and its version. Bumping the version is what makes an old
#: extraction comparable/superseded and what scopes the queue dedupe key.
EXTRACTOR = "ai_profile_extractor"
EXTRACTOR_VERSION = "1.0.0"
#: Tied to the PROFILE_SCHEMA in resume_parser — a schema change is a new prompt.
PROMPT_VERSION = "profile_schema_v1"

#: Session states used by this scope (subset of ONBOARDING_STATES).
STATE_AWAITING_RESUME = "awaiting_resume"
STATE_PROCESSING = "resume_processing"
STATE_BLOCKED = "extraction_blocked"
STATE_REVIEW = "profile_review_required"

#: Queue statuses in which the work may still move without user action.
LIVE_JOB_STATUSES = ("queued", "processing", "paused", "needs_input")

#: Blocked codes (docs/contracts/04 §7). Each has exactly one fix.
BLOCKED_CODES = {
    "no_readable_text": {"retryable": False},
    "unsupported_media_type": {"retryable": False},
    "payload_too_large": {"retryable": False},
    "ai_unavailable": {"retryable": True},
    "ai_blocked": {"retryable": False},
    "guardrail_failed": {"retryable": False},
    "profile_missing": {"retryable": False},
    "quota_exhausted": {"retryable": False},
    "internal_error": {"retryable": True},
}

#: Delay before a user-requested retry becomes runnable. Zero: the parse quota
#: (enforced at upload, charged once per success) bounds hammering, and the
#: queue's own lease/backoff protects the provider from repeated runs.
RETRY_DELAY_SECONDS = 0

#: Session states → resume-gate progress percent (honest, monotonic).
_PROGRESS_PERCENT = {
    STATE_AWAITING_RESUME: 0,
    STATE_PROCESSING: 50,
    STATE_BLOCKED: 50,
    STATE_REVIEW: 100,
}

# --------------------------------------------------------------------------- #
# Error sanitisation — provider secrets never reach the database
# --------------------------------------------------------------------------- #
_SECRET_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    # Provider key shapes: sk-…, Bearer …, key=…/token=…
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (re.compile(r"(?i)\b(api[_-]?key|apikey|token|secret|password|authorization)\b\s*[:=]\s*\S+"),
     r"\1 ***"),
    (re.compile(r"(?i)\b(eyJ[A-Za-z0-9_\-]{20,})"), "***"),  # JWT-shaped
)


def safe_error_message(raw: Any, *, limit: int = 500) -> str:
    """Collapse an error into a safe, one-line, key-free human sentence.

    Control characters and newlines are collapsed (log/store injection), any
    key-shaped token is masked, and the result is truncated. This is the only
    function the onboarding flow may use to persist an error string.
    """
    text = " ".join(str(raw or "").split())[:limit * 3]
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def blocked_payload(code: str, message: str, *, retryable: Optional[bool] = None,
                    retry_after: Optional[int] = None) -> Dict[str, Any]:
    """The session's ``blocked`` document — ``code`` is never free text."""
    code = code if code in BLOCKED_CODES else "internal_error"
    return {
        "code": code,
        "message": safe_error_message(message),
        "retryable": BLOCKED_CODES[code]["retryable"] if retryable is None else bool(retryable),
        "retry_after": retry_after,
        "since": datetime.utcnow().isoformat() + "Z",
    }


# --------------------------------------------------------------------------- #
# Session management
# --------------------------------------------------------------------------- #
def get_session(db: Session, user_id: int) -> Optional[OnboardingSession]:
    return db.query(OnboardingSession).filter(OnboardingSession.user_id == user_id).first()


def get_session_by_id(db: Session, user_id: int, session_id: int) -> Optional[OnboardingSession]:
    """Tenant-scoped fetch: another user's session id is simply not found."""
    return (
        db.query(OnboardingSession)
        .filter(OnboardingSession.id == session_id, OnboardingSession.user_id == user_id)
        .first()
    )


def get_or_create_session(db: Session, user: User, *, request_id: str = "") -> OnboardingSession:
    """Idempotent session creation — exactly one row per user, ever."""
    session = get_session(db, user.id)
    if session:
        return session
    session = OnboardingSession(user_id=user.id, state=STATE_AWAITING_RESUME,
                                state_since=datetime.utcnow(), blocked={},
                                progress=progress_for(STATE_AWAITING_RESUME))
    db.add(session)
    try:
        db.commit()
    except Exception:
        # Lost a race against a parallel first read — the winner's row is fine.
        db.rollback()
        session = get_session(db, user.id)
        if session:
            return session
        raise
    db.refresh(session)
    record_event(db, session, "onboarding.started", actor_type="system_api",
                 actor_label="session created", request_id=request_id,
                 message="Onboarding session created")
    from app.core import audit

    audit.audit(db, "onboarding.started", user=user, user_id=user.id,
                target=f"session:{session.id}", detail={"session_id": session.id})
    inc("jobhunter_onboarding_sessions_total")
    return session


def record_event(
    db: Session,
    session: OnboardingSession,
    event_type: str,
    *,
    gate: Optional[str] = None,
    state_from: Optional[str] = None,
    state_to: Optional[str] = None,
    actor_type: str = "user",
    actor_label: str = "",
    pipeline_job_id: Optional[int] = None,
    request_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    severity: str = "info",
    message: str = "",
    commit: bool = False,
) -> OnboardingEvent:
    """Append one timeline row with the next per-session sequence.

    The session runs with ``autoflush=False``, so earlier uncommitted events
    must be flushed before ``max(sequence)`` is read, or two events appended in
    one transaction would collide on the unique (session, sequence) constraint.
    A savepoint retry covers the (rarer) cross-session race between the API
    and a worker without rolling back the caller's work.
    """
    from sqlalchemy.exc import IntegrityError

    db.flush()  # pending event inserts become visible to the max() below
    last = (
        db.query(OnboardingEvent.sequence)
        .filter(OnboardingEvent.session_id == session.id)
        .order_by(OnboardingEvent.sequence.desc())
        .first()
    )

    def _build(sequence: int) -> OnboardingEvent:
        return OnboardingEvent(
            user_id=session.user_id,
            session_id=session.id,
            sequence=sequence,
            event_type=event_type,
            gate=gate,
            state_from=state_from,
            state_to=state_to,
            actor_type=actor_type,
            actor_label=(actor_label or "")[:120],
            pipeline_job_id=pipeline_job_id,
            request_id=(request_id or "")[:64],
            payload=payload or {},
            severity=severity,
            message=(message or "")[:400],
        )

    event = _build(int(last[0] if last else 0) + 1)
    nested = db.begin_nested()
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        # Another session appended between our read and our insert — retry
        # once from the row that actually exists now.
        nested.rollback()
        db.expire_all()
        again = (
            db.query(OnboardingEvent.sequence)
            .filter(OnboardingEvent.session_id == session.id)
            .order_by(OnboardingEvent.sequence.desc())
            .first()
        )
        event = _build(int(again[0] if again else 0) + 1)
        db.add(event)
        db.flush()
    if commit:
        db.commit()
    return event


def _set_state(db: Session, session: OnboardingSession, new_state: str, *,
               actor_type: str = "system_worker", actor_label: str = "",
               event_type: str = "onboarding.state_changed",
               payload: Optional[Dict[str, Any]] = None,
               pipeline_job_id: Optional[int] = None) -> bool:
    """Transition the session state; append an event only when it actually moved."""
    if session.state == new_state:
        return False
    previous = session.state
    session.state = new_state
    session.state_since = datetime.utcnow()
    session.updated_at = datetime.utcnow()
    if pipeline_job_id is not None:
        session.pipeline_job_id = pipeline_job_id
    session.progress = progress_for(new_state)
    record_event(
        db, session, event_type,
        gate="resume", state_from=previous, state_to=new_state,
        actor_type=actor_type, actor_label=actor_label,
        pipeline_job_id=pipeline_job_id if pipeline_job_id is not None else session.pipeline_job_id,
        payload=payload or {},
    )
    inc("jobhunter_onboarding_state_transitions_total", from_state=previous, to_state=new_state)
    return True


def progress_for(state: str) -> Dict[str, Any]:
    """The resume gate's progress, derived — never stored as a counter."""
    percent = _PROGRESS_PERCENT.get(state, 0)
    return {
        "gates_required": 1,
        "gates_completed": 1 if percent >= 100 else 0,
        "gates_skipped": 0,
        "percent": percent,
    }


# --------------------------------------------------------------------------- #
# Upload registration (router calls this after storing the file)
# --------------------------------------------------------------------------- #
def register_upload(
    db: Session,
    user: User,
    session: OnboardingSession,
    *,
    filepath: str,
    filename: str,
    content_type: str,
    sha256_hex: str,
    size_bytes: int,
) -> Tuple[ResumeDocument, bool]:
    """Record the uploaded document (idempotent on identical bytes).

    Returns ``(document, created)``. A re-upload of the same bytes under the
    same role returns the existing row untouched — the caller then simply
    re-attaches to whatever extraction state that document is in, which is what
    makes an accidental double-click or a flaky network harmless.

    A *different* upload archives the previous master (row and file are kept)
    and supersedes any extraction still attached to it, so an in-flight worker
    discards its result instead of writing a profile for a replaced document.
    """
    sha256_hex = (sha256_hex or "").lower()
    existing = (
        db.query(ResumeDocument)
        .filter(ResumeDocument.user_id == user.id, ResumeDocument.sha256 == sha256_hex,
                ResumeDocument.role == "master")
        .first()
    )
    if existing:
        if existing.state == "archived":
            # Same bytes coming back after a replacement: reactivate the row.
            existing.state = "stored"
            existing.archived_at = None
            existing.error_code = None
            existing.error_message = None
            db.commit()
            db.refresh(existing)
        return existing, False

    # Archive every other active master — exactly one current master per user.
    for old in (
        db.query(ResumeDocument)
        .filter(ResumeDocument.user_id == user.id, ResumeDocument.role == "master",
                ResumeDocument.state != "archived")
        .all()
    ):
        old.state = "archived"
        old.archived_at = datetime.utcnow()
        superseded = (
            db.query(ResumeExtraction)
            .filter(ResumeExtraction.resume_document_id == old.id,
                    ResumeExtraction.state.in_(("pending", "running")))
            .all()
        )
        for extraction in superseded:
            extraction.state = "superseded"
            extraction.finished_at = datetime.utcnow()
            extraction.error_code = "superseded"
            extraction.error_message = "Document replaced by a newer upload"
            record_event(db, session, "extraction.superseded", gate="resume",
                         actor_type="user", actor_label="document replaced",
                         pipeline_job_id=extraction.pipeline_job_id,
                         payload={"extraction_id": extraction.id, "document_id": old.id},
                         message="Extraction superseded — resume replaced")
        old_resume = (
            db.query(Resume)
            .filter(Resume.user_id == user.id, Resume.filepath == old.filepath, Resume.type == "master")
            .first()
        )
        if old_resume:
            old_resume.status = "archived"

    document = ResumeDocument(
        user_id=user.id,
        role="master",
        state="stored",
        filename=filename[:300],
        display_name=filename[:300],
        filepath=filepath[:600],
        content_type=content_type[:80],
        size_bytes=int(size_bytes or 0),
        sha256=sha256_hex,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    session.resume_document_id = document.id
    session.updated_at = datetime.utcnow()
    record_event(db, session, "resume.uploaded", gate="resume", actor_type="user",
                 payload={"document_id": document.id, "sha256": document.sha256,
                          "size_bytes": document.size_bytes},
                 message=f"Uploaded {document.filename}")
    db.commit()
    return document, True


def extraction_dedupe_key(document_id: int) -> str:
    """Queue dedupe key — one live extraction per (document, extractor version)."""
    return f"extract:{document_id}:{EXTRACTOR_VERSION}"


def start_extraction(db: Session, user: User, session: OnboardingSession,
                     document: ResumeDocument, *, trigger: str = "user") -> ResumeExtraction:
    """Create the attempt row and enqueue the background job.

    The attempt exists *before* the model is called, so any crash leaves a
    recoverable row. The queue dedupe key reuses a finished row's budget slot
    (see ``job_queue.enqueue``), so a user retry cannot grow unbounded rows.
    """
    last_attempt = (
        db.query(ResumeExtraction.attempt)
        .filter(ResumeExtraction.resume_document_id == document.id)
        .order_by(ResumeExtraction.attempt.desc())
        .first()
    )
    extraction = ResumeExtraction(
        user_id=user.id,
        resume_document_id=document.id,
        attempt=int(last_attempt[0] if last_attempt else 0) + 1,
        kind="profile_extraction",
        state="pending",
        extractor=EXTRACTOR,
        extractor_version=EXTRACTOR_VERSION,
        prompt_version=PROMPT_VERSION,
        input_sha256=document.sha256,
        trigger=trigger,
    )
    db.add(extraction)
    db.commit()
    db.refresh(extraction)

    from app.services.job_queue import enqueue

    item = enqueue(
        db,
        user_id=user.id,
        pipeline=EXTRACTION_PIPELINE,
        payload={
            "task": EXTRACTION_TASK,
            "document_id": document.id,
            "extraction_id": extraction.id,
            "trigger": trigger,
        },
        priority=2,  # user is waiting on this one — ahead of discovery/email work
        dedupe_key=extraction_dedupe_key(document.id),
        delay_seconds=0 if trigger != "retry" else RETRY_DELAY_SECONDS,
    )
    extraction.pipeline_job_id = item.id if item else None
    document.state = "extracting"
    document.error_code = None
    document.error_message = None
    session.extraction_id = extraction.id
    session.pipeline_job_id = extraction.pipeline_job_id
    session.blocked = {}
    db.commit()
    db.refresh(extraction)

    record_event(db, session, "extraction.started", gate="resume", actor_type="system_worker",
                 pipeline_job_id=extraction.pipeline_job_id,
                 payload={"extraction_id": extraction.id, "attempt": extraction.attempt,
                          "trigger": trigger},
                 message=f"Extraction attempt {extraction.attempt} queued")
    _set_state(db, session, STATE_PROCESSING, actor_type="system_worker",
               event_type="onboarding.state_changed", pipeline_job_id=extraction.pipeline_job_id)
    db.commit()
    inc("jobhunter_onboarding_extractions_total", trigger=trigger)
    return extraction


def reattach_in_flight(db: Session, session: OnboardingSession,
                       document: ResumeDocument) -> bool:
    """Re-attach a session to a document's still-in-flight extraction.

    Used when identical bytes are re-uploaded: the queue dedupe key means the
    original work is unchanged, so the session simply points back at it.
    Returns ``True`` when there was something in flight to re-attach to.
    """
    extraction = (
        db.query(ResumeExtraction)
        .filter(ResumeExtraction.resume_document_id == document.id)
        .order_by(ResumeExtraction.attempt.desc())
        .first()
    )
    if not extraction or extraction.state not in ("pending", "running"):
        return False
    session.resume_document_id = document.id
    session.extraction_id = extraction.id
    session.pipeline_job_id = extraction.pipeline_job_id
    session.blocked = {}
    _set_state(db, session, STATE_PROCESSING, actor_type="system_api",
               pipeline_job_id=extraction.pipeline_job_id)
    db.commit()
    return True


def retry_blocked_extraction(db: Session, user: User, session: OnboardingSession) -> Tuple[bool, str]:
    """Re-run the blocked async gate (contract T8).

    Returns ``(ok, reason)``. Only a genuinely retryable blocked state qualifies:
    guardrail rejections and unreadable documents need different input, not a
    retry — the SPA re-uploads for those instead.
    """
    blocked = session.blocked if isinstance(session.blocked, dict) else {}
    if session.state != STATE_BLOCKED:
        return False, "not_blocked"
    if not blocked.get("retryable", False):
        return False, "not_retryable"
    document = (
        db.query(ResumeDocument)
        .filter(ResumeDocument.id == session.resume_document_id, ResumeDocument.user_id == user.id)
        .first()
    )
    if not document:
        return False, "document_missing"
    start_extraction(db, user, session, document, trigger="retry")
    record_event(db, session, "onboarding.retried", gate="resume", actor_type="user",
                 payload={"document_id": document.id}, message="User retried extraction")
    from app.core import audit

    audit.audit(db, "onboarding.retried", user=user, user_id=user.id,
                target=f"document:{document.id}",
                detail={"extraction_id": session.extraction_id})
    return True, "queued"


# --------------------------------------------------------------------------- #
# Reconciliation — the state is a function of the stored facts
# --------------------------------------------------------------------------- #
def _latest_extraction(db: Session, document: ResumeDocument) -> Optional[ResumeExtraction]:
    return (
        db.query(ResumeExtraction)
        .filter(ResumeExtraction.resume_document_id == document.id)
        .order_by(ResumeExtraction.attempt.desc(), ResumeExtraction.id.desc())
        .first()
    )


def reconcile_session(db: Session, user: User, session: OnboardingSession) -> OnboardingSession:
    """Correct the persisted state against the facts, on every read.

    This is the crash path (contract 04 §9.6): if the queue row went terminal
    without the handler finishing its bookkeeping — a worker OOM, a deploy, a
    lost enqueue — the read itself repairs the session, marks the attempt
    failed with a retryable blocked code, and (for a lost job row) re-enqueues
    the work. Idempotent: when the facts already agree, nothing is written.
    """
    document = (
        db.query(ResumeDocument)
        .filter(ResumeDocument.id == session.resume_document_id, ResumeDocument.user_id == user.id)
        .first()
    ) if session.resume_document_id else None

    if not document or document.state == "archived":
        # No live document — nothing can be in flight. (Replacement flows set
        # their own states; this only repairs a session whose document vanished
        # without one.)
        if not document:
            _set_state(db, session, STATE_AWAITING_RESUME, actor_type="system_api",
                       payload={"reason": "no_document"})
            db.commit()
        return session

    extraction = (
        db.query(ResumeExtraction).filter(ResumeExtraction.id == session.extraction_id).first()
        if session.extraction_id else None
    )
    if extraction is None or extraction.resume_document_id != document.id:
        # Defensive: never mix rows across documents — follow the document.
        extraction = _latest_extraction(db, document)

    if extraction is None:
        if document.state in ("stored", "extracting"):
            # A document with no attempt at all: the enqueue was lost — recover.
            start_extraction(db, user, session, document, trigger="recovery")
        return session

    if extraction.state == "succeeded":
        _set_state(db, session, STATE_REVIEW, actor_type="system_api",
                   payload={"extraction_id": extraction.id})
        db.commit()
        return session

    if extraction.state == "superseded":
        _set_state(db, session, STATE_AWAITING_RESUME, actor_type="system_api",
                   payload={"extraction_id": extraction.id, "reason": "superseded"})
        db.commit()
        return session

    if extraction.state == "rejected":
        session.blocked = blocked_payload(
            extraction.error_code or "guardrail_failed", extraction.error_message or "",
            retryable=False)
        _set_state(db, session, STATE_BLOCKED, actor_type="system_api",
                   payload={"extraction_id": extraction.id})
        db.commit()
        return session

    if extraction.state == "failed":
        code = extraction.error_code if extraction.error_code in BLOCKED_CODES else "internal_error"
        session.blocked = blocked_payload(
            code, extraction.error_message or "", retryable=BLOCKED_CODES[code]["retryable"])
        _set_state(db, session, STATE_BLOCKED, actor_type="system_api",
                   payload={"extraction_id": extraction.id})
        db.commit()
        return session

    # pending | running — is the queue work actually alive?
    job = (
        db.query(PipelineJob).filter(PipelineJob.id == extraction.pipeline_job_id).first()
        if extraction.pipeline_job_id else None
    )
    if job is not None and job.status in LIVE_JOB_STATUSES:
        session.blocked = {}
        _set_state(db, session, STATE_PROCESSING, actor_type="system_api",
                   payload={"extraction_id": extraction.id, "job_status": job.status})
        db.commit()
        return session

    if job is None:
        # The enqueue (or the row) was lost — worker restart / deploy window.
        # Re-enqueue under the same dedupe key; the handler's own idempotency
        # makes a double run harmless.
        log.warning("onboarding extraction %s lost its queue row — re-enqueueing", extraction.id)
        start_extraction(db, user, session, document, trigger="recovery")
        return session

    # The queue row went terminal (dead / failed / cancelled) without the
    # handler recording a failure: a hard worker crash. Mark the attempt and
    # park the session in a retryable blocked state — this is the acceptance
    # criterion "worker failure produces a retryable state".
    extraction.state = "failed"
    extraction.finished_at = extraction.finished_at or datetime.utcnow()
    extraction.error_code = extraction.error_code or "internal_error"
    extraction.error_message = safe_error_message(
        extraction.error_message or f"worker stopped before finishing (queue row {job.status})")
    session.blocked = blocked_payload(extraction.error_code, extraction.error_message)
    _set_state(db, session, STATE_BLOCKED, actor_type="system_watchdog",
               payload={"extraction_id": extraction.id, "job_status": job.status})
    record_event(db, session, "extraction.failed", gate="resume", actor_type="system_watchdog",
                 pipeline_job_id=job.id, severity="warning",
                 payload={"extraction_id": extraction.id, "queue_status": job.status},
                 message=extraction.error_message or "Extraction failed")
    db.commit()
    _notify_once(db, user.id, "onboarding_step_required",
                 "Resume processing needs attention",
                 "We couldn't finish extracting your resume. Open onboarding to retry.",
                 "/onboarding", {"extraction_id": extraction.id, "code": extraction.error_code})
    return session


# --------------------------------------------------------------------------- #
# Status document — the one shape every endpoint returns
# --------------------------------------------------------------------------- #
def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def _document_view(document: Optional[ResumeDocument]) -> Optional[Dict[str, Any]]:
    if not document:
        return None
    # ``filepath`` is deliberately absent — it is an opaque storage path.
    return {
        "id": document.id,
        "filename": document.filename,
        "state": document.state,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
        "sha256": document.sha256,
        "uploaded_at": _iso(document.created_at),
        "archived_at": _iso(document.archived_at),
    }


def _extraction_view(extraction: Optional[ResumeExtraction]) -> Optional[Dict[str, Any]]:
    if not extraction:
        return None
    return {
        "id": extraction.id,
        "attempt": extraction.attempt,
        "state": extraction.state,
        "pipeline_job_id": extraction.pipeline_job_id,
        "extractor": extraction.extractor,
        "extractor_version": extraction.extractor_version,
        "model": extraction.model,
        "prompt_version": extraction.prompt_version,
        "field_count": extraction.field_count,
        "tokens": extraction.tokens if isinstance(extraction.tokens, dict) else {},
        "latency_ms": extraction.latency_ms,
        "error_code": extraction.error_code,
        "error_message": extraction.error_message,
        "trigger": extraction.trigger,
        "started_at": _iso(extraction.started_at),
        "finished_at": _iso(extraction.finished_at),
        "created_at": _iso(extraction.created_at),
    }


def status_document(db: Session, user: User, session: OnboardingSession) -> Dict[str, Any]:
    """The full status shape (contract 13 §1, resume-gate scope).

    Every route in the onboarding router returns this, so a client never merges
    a partial answer into its own idea of the state.
    """
    document = (
        db.query(ResumeDocument)
        .filter(ResumeDocument.id == session.resume_document_id, ResumeDocument.user_id == user.id)
        .first()
    ) if session.resume_document_id else None
    extraction = (
        db.query(ResumeExtraction).filter(ResumeExtraction.id == session.extraction_id).first()
        if session.extraction_id else None
    )
    job = (
        db.query(PipelineJob).filter(PipelineJob.id == session.pipeline_job_id,
                                     PipelineJob.user_id == user.id).first()
        if session.pipeline_job_id else None
    )

    consents = dict(user.consents or {})
    consents_complete = bool(consents.get("terms_accepted_at")) and bool(consents.get("data_processing_accepted_at"))

    blocked = session.blocked if isinstance(session.blocked, dict) else {}
    live_work = None
    if job and job.status in LIVE_JOB_STATUSES:
        live_work = {
            "pipeline_job_id": job.id,
            "status": job.status,
            "attempts": job.attempts,
            "scheduled_at": _iso(job.scheduled_at),
        }

    next_action: Dict[str, Any]
    if session.state == STATE_REVIEW:
        next_action = {
            "gate": "resume", "label": "Review your extracted profile",
            "route": "/profile", "method": "GET",
            "entity": {"type": "profile", "id": extraction.profile_id if extraction else None},
        }
    elif session.state == STATE_BLOCKED:
        retryable = bool(blocked.get("retryable"))
        next_action = {
            "gate": "resume",
            "label": "Retry extraction" if retryable else "Upload a different resume",
            "route": "/onboarding/retry" if retryable else "/onboarding/resume",
            "method": "POST",
            "entity": {"type": "resume_document", "id": document.id if document else None},
        }
    elif session.state == STATE_PROCESSING:
        next_action = {
            "gate": "resume", "label": "Wait for extraction to finish",
            "route": "/onboarding/status", "method": "GET",
            "entity": {"type": "resume_document", "id": document.id if document else None},
        }
    else:
        next_action = {
            "gate": "resume", "label": "Upload your master resume",
            "route": "/onboarding/resume", "method": "POST",
            "entity": None,
        }

    return {
        "session_id": session.id,
        "state": session.state,
        "state_since": _iso(session.state_since),
        "progress": session.progress if isinstance(session.progress, dict) else progress_for(session.state),
        "checklist": [
            {"key": "account", "label": "Account created", "required": True, "status": "completed",
             "evidence": {"user_id": user.id}},
            {"key": "consents", "label": "Terms & data processing accepted", "required": True,
             "status": "completed" if consents_complete else "pending",
             "evidence": {"terms": consents.get("terms_accepted_at"),
                          "data_processing": consents.get("data_processing_accepted_at")}},
            {"key": "resume", "label": "Master resume uploaded & extracted", "required": True,
             "status": ("completed" if session.state == STATE_REVIEW
                        else "blocked" if session.state == STATE_BLOCKED
                        else "in_progress" if session.state == STATE_PROCESSING
                        else "pending"),
             "evidence": {"resume_document_id": document.id if document else None,
                          "extraction_id": extraction.id if extraction else None,
                          "pipeline_job_id": session.pipeline_job_id}},
        ],
        "blocked": {
            "code": blocked.get("code"),
            "message": blocked.get("message"),
            "retryable": bool(blocked.get("retryable")),
            "retry_after": blocked.get("retry_after"),
            "since": blocked.get("since"),
        },
        "next_action": next_action,
        "resume_document": _document_view(document),
        "extraction": _extraction_view(extraction),
        "live_work": live_work,
        "consents": {
            "terms": consents.get("terms_accepted_at"),
            "data_processing": consents.get("data_processing_accepted_at"),
        },
        "completed_at": _iso(session.completed_at),
        "server_time": _iso(datetime.utcnow()),
    }


# --------------------------------------------------------------------------- #
# Notifications — one per (extraction, kind), never a duplicate
# --------------------------------------------------------------------------- #
def _notify_once(db: Session, user_id: int, kind: str, title: str, body: str,
                 link: str, meta: Dict[str, Any]) -> bool:
    """One notification per (extraction, kind) — a retried/replayed run never re-notifies.

    The dedupe marker is the extraction id in the notification's meta, so the
    check survives worker restarts (it reads the table, not process memory).
    """
    from app.api.routers.notifications import create_notification
    from app.models.models import Notification

    extraction_id = meta.get("extraction_id")
    if extraction_id is not None:
        rows = (
            db.query(Notification)
            .filter(Notification.user_id == user_id, Notification.kind == kind)
            .order_by(Notification.id.desc())
            .limit(50)
            .all()
        )
        for row in rows:
            row_meta = row.meta if isinstance(row.meta, dict) else {}
            if row_meta.get("extraction_id") == extraction_id:
                return False
    created = create_notification(db, user_id, kind, title[:200], body=body[:2000],
                                  link=link, meta=meta)
    if created:
        inc("jobhunter_onboarding_notifications_total", kind=kind)
    return bool(created)


# --------------------------------------------------------------------------- #
# Outcome application — shared by the handler and the reconciliation
# --------------------------------------------------------------------------- #
def _profile_upsert(db: Session, user_id: int, extraction: ResumeExtraction,
                    profile_data: Dict[str, Any], layout: Dict[str, Any],
                    resume_id: int) -> Profile:
    """Idempotent profile write keyed on ``source_extraction_id``.

    Reprocessing the same document (a duplicate job, a worker retry, a
    lease-recovery re-run) finds the row this extraction already created and
    updates it — it can never insert a second one. A genuinely new extraction
    replaces the previous active profile, which is exactly what the legacy
    synchronous upload did (delete-all, insert-one).
    """
    existing = (
        db.query(Profile)
        .filter(Profile.user_id == user_id, Profile.source_extraction_id == extraction.id)
        .first()
    )
    if existing:
        existing.data = profile_data
        existing.layout = layout
        existing.master_resume_id = resume_id
        existing.extraction_source = "ai"
        db.commit()
        db.refresh(existing)
        return existing

    # Legacy rows (source_extraction_id IS NULL) and older extractions are
    # replaced, not accumulated — the product reads "the" profile everywhere.
    # Detach the historical extractions' profile pointers first: they are the
    # only FK into ``profiles``, and an append-only extraction row must survive
    # the replacement of the profile it once produced.
    from app.models.models import ResumeExtraction

    doomed = (
        db.query(Profile.id).filter(Profile.user_id == user_id,
                                    Profile.source_extraction_id != extraction.id).all()
    )
    doomed_ids = [row[0] for row in doomed]
    if doomed_ids:
        db.query(ResumeExtraction).filter(ResumeExtraction.profile_id.in_(doomed_ids)).update(
            {ResumeExtraction.profile_id: None}, synchronize_session=False)
        db.query(Profile).filter(Profile.user_id == user_id).delete(synchronize_session=False)
    db.flush()
    profile = Profile(
        user_id=user_id,
        data=profile_data,
        layout=layout,
        master_resume_id=resume_id,
        extraction_source="ai",
        source_extraction_id=extraction.id,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _resume_upsert(db: Session, user_id: int, document: ResumeDocument,
                   profile_data: Dict[str, Any], layout: Dict[str, Any]) -> Resume:
    """Create (once) and refresh the product-facing master ``Resume`` row.

    Keyed on (user, filepath) — one master resume row per uploaded document,
    so a duplicate run reuses the row instead of inserting a second one. The
    file itself is the *original* upload, owned by the ``ResumeDocument``.
    """
    resume = (
        db.query(Resume)
        .filter(Resume.user_id == user_id, Resume.filepath == document.filepath,
                Resume.type == "master")
        .first()
    )
    if not resume:
        resume = Resume(user_id=user_id, filename=document.filename, filepath=document.filepath,
                        type="master", tags=["master"])
        db.add(resume)
    resume.status = "approved"
    resume.profile_snapshot = profile_data
    resume.layout = layout
    resume.display_name = document.display_name or document.filename
    resume.text_snapshot = render_profile_text(profile_data)
    resume.approved_at = resume.approved_at or datetime.utcnow()
    db.commit()
    db.refresh(resume)
    return resume


def apply_extraction_success(
    db: Session,
    user: User,
    session: OnboardingSession,
    document: ResumeDocument,
    extraction: ResumeExtraction,
    *,
    profile_data: Dict[str, Any],
    meta: Dict[str, Any],
    text: str,
    layout: Dict[str, Any],
    latency_ms: int = 0,
) -> Dict[str, Any]:
    """Commit a successful extraction — idempotent by construction.

    Order matters: every durable write happens before the queue item is
    allowed to complete, and the guard at the top of the handler means this
    function runs at most once per extraction row.
    """
    profile_data = dict(profile_data or {})
    profile_data["raw_text"] = (text or "")[:20000]

    resume = _resume_upsert(db, user.id, document, profile_data, layout)
    profile = _profile_upsert(db, user.id, extraction, profile_data, layout, resume.id)

    output_json = json.dumps(profile_data, sort_keys=True, default=str)
    was_succeeded = extraction.state == "succeeded"
    extraction.state = "succeeded"
    extraction.finished_at = datetime.utcnow()
    extraction.model = str((meta or {}).get("model") or (meta or {}).get("source") or "")[:120]
    extraction.output_sha256 = hashlib.sha256(output_json.encode()).hexdigest()
    extraction.field_count = sum(
        len(profile_data.get(key) or []) for key in ("skills", "experience", "education", "projects")
    ) + (1 if profile_data.get("name") else 0)
    extraction.profile_id = profile.id
    extraction.latency_ms = int((meta or {}).get("latency_ms") or latency_ms or 0)
    usage = (meta or {}).get("usage") or {}
    extraction.tokens = {
        "prompt": int(usage.get("prompt_tokens") or 0),
        "completion": int(usage.get("completion_tokens") or 0),
        "total": int(usage.get("total_tokens") or 0),
    }
    guardrail = (meta or {}).get("guardrail") or {}
    extraction.guardrail_report = guardrail if isinstance(guardrail, dict) else {}

    document.state = "approved"
    document.profile_snapshot = profile_data
    document.layout = layout
    document.text_sha256 = hashlib.sha256((text or "").encode()).hexdigest()
    document.error_code = None
    document.error_message = None
    db.commit()

    from app.api.deps import invalidate_context

    invalidate_context(user.id)

    if not was_succeeded:
        # Quota + audit only on the first success of this attempt — a duplicate
        # run (idempotent replay) must not double-charge the user's budget.
        try:
            from app.core.entitlements import increment_usage

            increment_usage(db, user.id, "resume_parses_per_month", 1)
        except Exception as exc:  # pragma: no cover - ledger failure ≠ lost work
            log.warning("could not charge resume parse quota for extraction %s: %s", extraction.id, exc)
        from app.core import audit

        audit.audit(db, "extraction.succeeded", user=user, user_id=user.id,
                    target=f"document:{document.id}",
                    detail={"extraction_id": extraction.id, "resume_id": resume.id,
                            "profile_id": profile.id, "attempt": extraction.attempt,
                            "extractor_version": EXTRACTOR_VERSION})

    _set_state(db, session, STATE_REVIEW, actor_type="system_worker",
               event_type="onboarding.state_changed", pipeline_job_id=extraction.pipeline_job_id)
    record_event(db, session, "extraction.succeeded", gate="resume", actor_type="system_worker",
                 pipeline_job_id=extraction.pipeline_job_id,
                 payload={"extraction_id": extraction.id, "resume_id": resume.id,
                          "profile_id": profile.id, "field_count": extraction.field_count},
                 message="Resume extracted — profile ready to review")
    db.commit()

    _notify_once(
        db, user.id, "profile_review_required",
        "Your resume was processed",
        f"We extracted {extraction.field_count} fields from {document.filename}. "
        "Review your profile to finish onboarding.",
        "/profile", {"extraction_id": extraction.id, "document_id": document.id},
    )

    # The existing tag pipeline is queued work too — unchanged behaviour.
    from app.services.job_queue import enqueue

    enqueue(db, user_id=user.id, pipeline="ai",
            payload={"task": "tag_resume", "resume_id": resume.id},
            priority=4, dedupe_key=f"tag_resume:{resume.id}")

    return {"status": "succeeded", "document_id": document.id, "extraction_id": extraction.id,
            "resume_id": resume.id, "profile_id": profile.id,
            "duplicate": was_succeeded}


def record_extraction_failure(
    db: Session,
    session: OnboardingSession,
    document: ResumeDocument,
    extraction: ResumeExtraction,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    """Mark the attempt failed and park the session in a retryable blocked state."""
    if extraction.state in ("succeeded", "superseded"):
        return  # never overwrite a finished verdict
    extraction.state = "failed"
    extraction.finished_at = datetime.utcnow()
    extraction.error_code = code if code in BLOCKED_CODES else "internal_error"
    extraction.error_message = safe_error_message(message)
    if document.state == "extracting":
        document.state = "stored"  # file is intact; a retry can re-drive it
    db.commit()

    session.blocked = blocked_payload(extraction.error_code, extraction.error_message,
                                      retryable=retryable)
    _set_state(db, session, STATE_BLOCKED, actor_type="system_worker",
               payload={"extraction_id": extraction.id})
    record_event(db, session, "extraction.failed", gate="resume", actor_type="system_worker",
                 pipeline_job_id=extraction.pipeline_job_id, severity="error",
                 payload={"extraction_id": extraction.id, "code": extraction.error_code},
                 message=extraction.error_message or "Extraction failed")
    db.commit()
    _notify_once(db, session.user_id, "onboarding_step_required",
                 "Resume processing needs attention",
                 extraction.error_message or "We couldn't extract your resume. Open onboarding to retry.",
                 "/onboarding", {"extraction_id": extraction.id, "code": extraction.error_code})


def record_extraction_rejected(
    db: Session,
    session: OnboardingSession,
    document: ResumeDocument,
    extraction: ResumeExtraction,
    *,
    code: str,
    message: str,
) -> None:
    """The answer (or the document) was refused — not retryable as-is."""
    if extraction.state in ("succeeded", "superseded"):
        return
    extraction.state = "rejected"
    extraction.finished_at = datetime.utcnow()
    extraction.error_code = code if code in BLOCKED_CODES else "guardrail_failed"
    extraction.error_message = safe_error_message(message)
    document.state = "rejected"
    document.error_code = extraction.error_code
    document.error_message = extraction.error_message
    db.commit()

    session.blocked = blocked_payload(extraction.error_code, extraction.error_message,
                                      retryable=False)
    _set_state(db, session, STATE_BLOCKED, actor_type="system_worker",
               payload={"extraction_id": extraction.id})
    record_event(db, session, "extraction.rejected_by_guardrail", gate="resume",
                 actor_type="system_worker", pipeline_job_id=extraction.pipeline_job_id,
                 severity="error",
                 payload={"extraction_id": extraction.id, "code": extraction.error_code},
                 message=extraction.error_message or "Extraction rejected")
    db.commit()
    _notify_once(db, session.user_id, "onboarding_step_required",
                 "Your resume needs attention",
                 extraction.error_message or "We couldn't use that document. Upload a text-based PDF or DOCX.",
                 "/onboarding", {"extraction_id": extraction.id, "code": extraction.error_code})


# --------------------------------------------------------------------------- #
# The queue handler — runs in the worker, never in the request
# --------------------------------------------------------------------------- #
async def handle_extraction_job(db: Session, item: PipelineJob) -> Dict[str, Any]:
    """``extraction`` pipeline handler: drive one extraction attempt.

    Idempotency contract (acceptance: "duplicate job execution is idempotent"):

    * an attempt already ``succeeded`` replays its result and writes nothing;
    * an attempt ``superseded`` (document replaced) writes nothing;
    * the profile/resume writes are upserts keyed on the extraction, so even a
      crash between writes cannot produce duplicate rows on re-run.

    Transient AI outages are raised for the worker to ``pause`` (no failure
    budget consumed; the watchdog resumes when the provider is back) — the
    attempt row stays ``running`` because the *same attempt* continues. A
    needs-action AI failure (no key, bad key, quota) is recorded and re-raised
    so the queue dead-letters it: the session then shows a non-retryable block
    with the fix.
    """
    from app.services.resume_parser import ai_extract_profile, extract_layout, extract_text

    payload = dict(item.payload or {})
    if payload.get("task") != EXTRACTION_TASK:
        return {"status": "noop", "reason": "unknown_task"}

    user = db.query(User).filter(User.id == item.user_id).first()
    if not user:
        raise RuntimeError("user_missing")
    # get_or_create (not get): a session row always exists for an in-flight
    # extraction — it is created before the upload is enqueued — but if the
    # row is somehow gone (manual tampering) the handler must still have a
    # session to park its state in rather than crash-looping the item.
    session = get_or_create_session(db, user)
    document = (
        db.query(ResumeDocument)
        .filter(ResumeDocument.id == payload.get("document_id"), ResumeDocument.user_id == user.id)
        .first()
    )
    extraction = (
        db.query(ResumeExtraction)
        .filter(ResumeExtraction.id == payload.get("extraction_id"), ResumeExtraction.user_id == user.id)
        .first()
    )
    if not document or not extraction:
        raise RuntimeError("extraction_rows_missing")

    # ---- idempotency guards -------------------------------------------------
    if extraction.state == "superseded":
        log.info("extraction %s superseded — skipping (document replaced)", extraction.id)
        return {"status": "superseded", "extraction_id": extraction.id}
    if extraction.state == "succeeded":
        profile = (
            db.query(Profile)
            .filter(Profile.user_id == user.id, Profile.source_extraction_id == extraction.id)
            .first()
        )
        return {"status": "succeeded", "extraction_id": extraction.id,
                "resume_document_id": document.id,
                "profile_id": profile.id if profile else None, "duplicate": True}

    started = datetime.utcnow()
    extraction.state = "running"
    extraction.started_at = extraction.started_at or started
    document.state = "extracting"
    db.commit()

    # ---- deterministic extraction (cheap, always safe to re-run) -----------
    try:
        text = extract_text(document.filepath)
    except Exception as exc:
        record_extraction_failure(db, session, document, extraction,
                                  code="no_readable_text", message=str(exc), retryable=False)
        return {"status": "rejected", "code": "no_readable_text",
                "message": extraction.error_message, "extraction_id": extraction.id}
    if not (text or "").strip():
        record_extraction_rejected(db, session, document, extraction,
                                   code="no_readable_text",
                                   message="No readable text found in the document (is it a scanned image?)")
        return {"status": "rejected", "code": "no_readable_text",
                "message": extraction.error_message, "extraction_id": extraction.id}

    layout = (
        extract_layout(document.filepath)
        if document.filename.lower().endswith(".pdf")
        else {"bullet_style": "•", "colors": [], "fonts": ["Calibri"], "estimated": True}
    )
    document.layout = layout
    db.commit()

    # ---- the AI extraction (the only slow, pausable part) ------------------
    call_started = datetime.utcnow()
    try:
        profile_data, meta = await ai_extract_profile(text, ai_config=None, db=db, user_id=user.id)
    except Exception as exc:
        latency_ms = int((datetime.utcnow() - call_started).total_seconds() * 1000)
        from app.services.ai_client import is_ai_error, is_transient_ai_error
        from app.services.ai_guardrails import GuardrailError

        if isinstance(exc, GuardrailError):
            record_extraction_rejected(db, session, document, extraction,
                                       code="guardrail_failed", message=str(exc))
            return {"status": "rejected", "code": "guardrail_failed",
                    "message": extraction.error_message, "extraction_id": extraction.id}
        if is_ai_error(exc):
            code = "ai_unavailable" if is_transient_ai_error(exc) else "ai_blocked"
            # Record the diagnosis (sanitised) for the status endpoint, but let
            # the exception propagate: the worker owns pause/fail accounting.
            extraction.error_code = code
            extraction.error_message = safe_error_message(str(exc))
            db.commit()
            if not is_transient_ai_error(exc):
                # Needs action (no key / bad key / quota): the worker will
                # dead-letter this item; park the session now so the user sees
                # the fix instead of an eternal spinner.
                record_extraction_failure(db, session, document, extraction,
                                          code=code, message=str(exc), retryable=False)
            raise
        # Non-AI crash: record for diagnosis; the queue retries with backoff.
        extraction.error_code = "internal_error"
        extraction.error_message = safe_error_message(f"{type(exc).__name__}: {exc}")
        db.commit()
        raise

    latency_ms = int((datetime.utcnow() - call_started).total_seconds() * 1000)
    if isinstance(meta, dict) and meta.get("latency_ms"):
        latency_ms = int(meta["latency_ms"])

    # A replacement may have superseded this attempt *while* the model ran.
    db.refresh(extraction)
    if extraction.state == "superseded":
        log.info("extraction %s superseded mid-run — discarding result", extraction.id)
        return {"status": "superseded", "extraction_id": extraction.id}

    result = apply_extraction_success(
        db, user, session, document, extraction,
        profile_data=profile_data, meta=meta if isinstance(meta, dict) else {},
        text=text, layout=layout, latency_ms=latency_ms,
    )
    # Best-effort search-context rebuild: its failure must not fail the
    # extraction (the context rebuilds lazily on its next real use).
    try:
        from app.api.deps import search_context

        await search_context(db, user.id, force_refresh=True)
    except Exception as exc:  # pragma: no cover - depends on AI availability
        log.info("search context rebuild after extraction %s deferred: %s", extraction.id,
                 safe_error_message(exc, limit=120))
    return result
