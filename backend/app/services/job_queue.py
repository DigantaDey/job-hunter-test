"""
Durable, tenant-scoped work queue for the pipelines.

Why not ``asyncio.create_task`` in the request handler (the v1.2 approach)? A
rolling deploy, a crash or a restart silently loses in-flight work. This queue
persists every unit of work in ``pipeline_jobs`` and gives workers:

* atomic claim with a lease (``status=processing`` + ``lease_expires_at``);
* retries with exponential backoff and a dead-letter state (``dead``);
* stall recovery — expired leases are re-queued;
* dedupe keys so re-running discovery/apply cannot double-enqueue;
* per-tenant scoping so a worker only ever touches its user's rows.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc, set_gauge
from app.models.models import PipelineJob

log = get_logger("app.queue")

PIPELINES = ("discovery", "application", "email", "funding", "ai")


def worker_id() -> str:
    if settings.worker_id:
        return settings.worker_id
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def enqueue(
    db: Session,
    *,
    user_id: int,
    pipeline: str,
    payload: Optional[Dict[str, Any]] = None,
    job_id: Optional[int] = None,
    priority: int = 5,
    dedupe_key: str = "",
    delay_seconds: int = 0,
    max_attempts: Optional[int] = None,
) -> Optional[PipelineJob]:
    """
    Add a work item. Returns ``None`` when an identical item is already queued
    (dedupe key), which keeps repeated user clicks idempotent.
    """
    if pipeline not in PIPELINES:
        raise ValueError(f"unknown pipeline '{pipeline}'")

    # ``uq_pipeline_user_dedupe`` is a HARD unique constraint over (user_id,
    # dedupe_key) — it covers finished rows too. Re-running a scan therefore has
    # to *reuse* the completed row instead of inserting a second one, otherwise
    # the second click raises IntegrityError and the API answers 500.
    if dedupe_key:
        existing = (
            db.query(PipelineJob)
            .filter(PipelineJob.user_id == user_id, PipelineJob.dedupe_key == dedupe_key)
            .first()
        )
        if existing:
            if existing.status in ("queued", "processing", "needs_input"):
                return None  # identical work is already in flight
            existing.pipeline = pipeline
            existing.job_id = job_id
            existing.payload = payload or {}
            existing.status = "queued"
            existing.priority = max(1, min(9, priority))
            existing.attempts = 0
            existing.max_attempts = max_attempts or settings.worker_max_attempts
            existing.scheduled_at = datetime.utcnow() + timedelta(seconds=max(0, delay_seconds))
            existing.lease_expires_at = None
            existing.locked_by = ""
            existing.finished_at = None
            existing.error = ""
            db.commit()
            db.refresh(existing)
            inc("jobhunter_queue_enqueued_total", pipeline=pipeline, reused="true")
            return existing

    item = PipelineJob(
        user_id=user_id,
        pipeline=pipeline,
        job_id=job_id,
        payload=payload or {},
        status="queued",
        priority=max(1, min(9, priority)),
        dedupe_key=dedupe_key[:300],
        scheduled_at=datetime.utcnow() + timedelta(seconds=max(0, delay_seconds)),
        max_attempts=max_attempts or settings.worker_max_attempts,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race against another worker on the same dedupe key — the other
        # enqueue already created the row, so behave like the dedupe path.
        db.rollback()
        return (
            db.query(PipelineJob)
            .filter(PipelineJob.user_id == user_id, PipelineJob.dedupe_key == dedupe_key)
            .first()
        )
    db.refresh(item)
    inc("jobhunter_queue_enqueued_total", pipeline=pipeline)
    return item


def claim(db: Session, *, pipelines: Sequence[str], lease_seconds: Optional[int] = None) -> Optional[PipelineJob]:
    """Atomically claim the next runnable item (priority, then arrival order)."""
    now = datetime.utcnow()
    lease = timedelta(seconds=lease_seconds or settings.worker_lease_seconds)
    candidate_id = (
        db.query(PipelineJob.id)
        .filter(
            PipelineJob.pipeline.in_(list(pipelines)),
            PipelineJob.status == "queued",
            PipelineJob.scheduled_at <= now,
        )
        .order_by(PipelineJob.priority.asc(), PipelineJob.scheduled_at.asc(), PipelineJob.id.asc())
        .limit(1)
        .scalar()
    )
    if candidate_id is None:
        return None

    updated = (
        db.query(PipelineJob)
        .filter(PipelineJob.id == candidate_id, PipelineJob.status == "queued")
        .update(
            {
                PipelineJob.status: "processing",
                PipelineJob.locked_by: worker_id(),
                PipelineJob.lease_expires_at: now + lease,
                PipelineJob.attempts: PipelineJob.attempts + 1,
                PipelineJob.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not updated:
        return None  # another worker won the race
    # populate_existing(): the UPDATE bypassed the identity map, so re-read the
    # row rather than returning a stale in-session object.
    return (
        db.query(PipelineJob)
        .populate_existing()
        .filter(PipelineJob.id == candidate_id)
        .first()
    )


def claim_item(db: Session, item_id: int, *, lease_seconds: Optional[int] = None) -> Optional[PipelineJob]:
    """
    Claim a *specific* item (used by the worker when re-running a known id).

    Returns ``None`` when the item is not claimable (already processing, done, or
    scheduled for the future) so two workers can never run the same item.
    """
    now = datetime.utcnow()
    updated = (
        db.query(PipelineJob)
        .filter(PipelineJob.id == item_id, PipelineJob.status == "queued", PipelineJob.scheduled_at <= now)
        .update(
            {
                PipelineJob.status: "processing",
                PipelineJob.locked_by: worker_id(),
                PipelineJob.lease_expires_at: now + timedelta(seconds=lease_seconds or settings.worker_lease_seconds),
                PipelineJob.attempts: PipelineJob.attempts + 1,
                PipelineJob.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not updated:
        return None
    return db.query(PipelineJob).populate_existing().filter(PipelineJob.id == item_id).first()


def complete(db: Session, item: PipelineJob, *, result: Optional[Dict[str, Any]] = None) -> None:
    item.status = "done"
    item.finished_at = datetime.utcnow()
    item.lease_expires_at = None
    item.locked_by = ""
    item.error = ""
    if result is not None:
        item.payload = {**(item.payload or {}), "result": result}
    db.commit()
    inc("jobhunter_queue_completed_total", pipeline=item.pipeline)


def fail(db: Session, item: PipelineJob, error: str, *, retryable: bool = True) -> str:
    """Record a failure; retry with backoff or move to the dead-letter state."""
    item.error = (error or "unknown error")[:2000]
    item.locked_by = ""
    item.lease_expires_at = None
    if retryable and item.attempts < (item.max_attempts or settings.worker_max_attempts):
        delay = min(600, int(settings.ai_backoff_base ** item.attempts * 5))
        item.status = "queued"
        item.scheduled_at = datetime.utcnow() + timedelta(seconds=delay)
        item.updated_at = datetime.utcnow()
        db.commit()
        inc("jobhunter_queue_retries_total", pipeline=item.pipeline)
        log.warning("queue item %s retry in %ss: %s", item.id, delay, item.error)
        return "retrying"
    item.status = "dead"
    item.finished_at = datetime.utcnow()
    db.commit()
    inc("jobhunter_queue_dead_total", pipeline=item.pipeline)
    log.error("queue item %s dead-lettered: %s", item.id, item.error)
    return "dead"


def needs_input(db: Session, item: PipelineJob, *, reason: str = "") -> None:
    item.status = "needs_input"
    item.error = reason[:2000]
    item.locked_by = ""
    item.lease_expires_at = None
    db.commit()


def recover_stalled(db: Session, *, pipelines: Optional[Sequence[str]] = None) -> int:
    """Re-queue items whose lease expired (worker crash / deploy)."""
    now = datetime.utcnow()
    query = db.query(PipelineJob).filter(
        PipelineJob.status == "processing",
        or_(PipelineJob.lease_expires_at.is_(None), PipelineJob.lease_expires_at < now),
    )
    if pipelines:
        query = query.filter(PipelineJob.pipeline.in_(list(pipelines)))
    stalled = query.all()
    for item in stalled:
        if item.attempts >= (item.max_attempts or settings.worker_max_attempts):
            item.status = "dead"
            item.finished_at = now
            item.error = item.error or "lease expired"
        else:
            item.status = "queued"
            item.scheduled_at = now
        item.locked_by = ""
        item.lease_expires_at = None
    if stalled:
        db.commit()
        inc("jobhunter_queue_recovered_total", value=len(stalled))
        log.warning("recovered %s stalled queue item(s)", len(stalled))
    return len(stalled)


def queue_stats(db: Session, *, user_id: Optional[int] = None) -> Dict[str, Dict[str, int]]:
    query = db.query(PipelineJob.pipeline, PipelineJob.status, func.count(PipelineJob.id))
    if user_id is not None:
        query = query.filter(PipelineJob.user_id == user_id)
    rows = query.group_by(PipelineJob.pipeline, PipelineJob.status).all()
    stats: Dict[str, Dict[str, int]] = {
        pipeline: {"queued": 0, "processing": 0, "done": 0, "failed": 0, "needs_input": 0, "dead": 0}
        for pipeline in PIPELINES
    }
    for pipeline, status, count in rows:
        bucket = stats.setdefault(pipeline, {"queued": 0, "processing": 0, "done": 0, "failed": 0, "needs_input": 0, "dead": 0})
        bucket[status] = int(count)
    set_gauge("jobhunter_queue_depth", sum(b.get("queued", 0) for b in stats.values()), scope="global")
    return stats


def recent_items(db: Session, *, user_id: int, pipeline: Optional[str] = None, status: Optional[str] = None,
                 limit: int = 50) -> List[PipelineJob]:
    query = db.query(PipelineJob).filter(PipelineJob.user_id == user_id)
    if pipeline:
        query = query.filter(PipelineJob.pipeline == pipeline)
    if status:
        query = query.filter(PipelineJob.status == status)
    return query.order_by(PipelineJob.created_at.desc()).limit(limit).all()
