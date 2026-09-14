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

Retry accounting — the contract (v2.2.2)
----------------------------------------
``PipelineJob.attempts`` counts **handler failures**, nothing else:

* :func:`claim` / :func:`claim_item` take a lease and leave ``attempts``
  alone. Being handed to a worker is not a failure, and a claim that ends in
  a pause or a crash must not spend budget the work never used.
* :func:`fail` increments ``attempts`` *first* and then applies the budget:
  ``attempts >= max_attempts`` → ``dead``, otherwise re-queue with backoff.
  So ``max_attempts=3`` means exactly three recorded failures, and the third
  one is the one that dead-letters.
* :func:`pause` (transient AI outage) consumes **no** attempt — an outage is
  not the work's fault. It has its own, separate safety valve: the
  ``paused_count`` in the payload, capped at :data:`AI_PAUSE_MAX`.
* :func:`recover_stalled` applies the same rule to an expired lease: a
  stalled item whose *failure* count has reached ``max_attempts`` is
  dead-lettered, otherwise it is re-queued.

The two budgets are independent by design. ``attempts``/``max_attempts``
answers "how many times has this work actually broken?"; ``paused_count``/
``AI_PAUSE_MAX`` answers "how many outages has it sat through?". Mixing them
(the pre-v2.2.2 behaviour, which incremented ``attempts`` at claim time) let
a pause → resume → claim cycle spend failure budget without a single failure,
so an item with ``max_attempts=3`` dead-lettered on its *first* real failure
after three outage cycles.
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

    ``max_attempts`` is the *failure* budget (default
    ``settings.worker_max_attempts``): the item is dead-lettered once
    :func:`fail` has recorded that many failures. Reusing a finished row
    (a user-driven re-run) resets both budgets — ``attempts`` to 0 and, with
    the payload, ``paused_count``.
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
            if existing.status in ("queued", "processing", "needs_input", "paused"):
                return None  # identical work is already in flight (paused = waiting on AI)
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
    """Atomically claim the next runnable item (priority, then arrival order).

    Claiming takes a lease only — it does **not** increment ``attempts``,
    which counts handler failures (see :func:`fail` and the module
    docstring). A pause/resume or crash/re-queue cycle therefore costs the
    item nothing.
    """
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

    Like :func:`claim`, this takes a lease only — ``attempts`` counts handler
    failures and is left untouched.
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


#: Safety valve: a paused item is re-run by the watchdog whenever the probe is
#: green. If the *same* work keeps failing on retry (a 400 the probe cannot
#: see, a provider that only fails on real prompts, …) it must not loop
#: forever — after this many pauses it is dead-lettered with its last error.
#:
#: This is the **pause budget**, tracked as ``paused_count`` in the payload and
#: deliberately separate from ``attempts``/``max_attempts`` (the *failure*
#: budget): an outage must not spend a work item's failure budget, and a work
#: item's failures must not spend its outage budget.
AI_PAUSE_MAX = 12


def pause(db: Session, item: PipelineJob, error: str) -> str:
    """Pause work because the AI layer is in a *transient outage*.

    The item is re-queued with backoff (``status='paused'``, ``scheduled_at``
    in the future) but — unlike :func:`fail` — the pause does NOT consume an
    attempt: an outage is not the work's fault, so retries caused by the
    outage cannot dead-letter innocent items. The watchdog (or a one-click
    resume) flips paused items back to ``queued`` when the availability
    signal is green. Work must be idempotent to be safe to pause this way
    (see the audit in the v2.1 CHANGELOG: every queued AI path creates its
    side effects only after the AI call succeeds, or upserts by unique key).

    The whole cycle is free, not just this call: ``drain_paused`` re-queues
    the item and :func:`claim` takes a lease without touching ``attempts``, so
    N pause/resume cycles leave the failure budget exactly as they found it
    (v2.2.2 — claiming used to increment it, which let an outage spend budget
    the work never used). The only cap on this path is :data:`AI_PAUSE_MAX`,
    counted separately in the payload's ``paused_count``.
    """
    # (mypy sees ORM attribute assignments as Column-typed — same known noise
    # class as fail()/complete(); ignored here to keep the mypy delta at zero.)
    item.error = (error or "ai_transient_outage")[:2000]  # type: ignore[assignment]
    item.locked_by = ""  # type: ignore[assignment]
    item.lease_expires_at = None  # type: ignore[assignment]
    payload = dict(item.payload or {})
    pauses = int(payload.get("paused_count") or 0) + 1
    if pauses > AI_PAUSE_MAX:
        item.status = "dead"  # type: ignore[assignment]
        item.finished_at = datetime.utcnow()  # type: ignore[assignment]
        item.error = f"paused {AI_PAUSE_MAX} times during AI outages, giving up: {item.error}"  # type: ignore[assignment]
        db.commit()
        inc("jobhunter_queue_dead_total", pipeline=item.pipeline)
        log.error("queue item %s dead-lettered after repeated AI outages", item.id)
        return "dead"
    payload["paused_count"] = pauses
    delay = min(600, int(settings.ai_backoff_base ** min(pauses, 8) * 5))
    item.payload = payload  # type: ignore[assignment]
    item.status = "paused"  # type: ignore[assignment]
    item.scheduled_at = datetime.utcnow() + timedelta(seconds=delay)  # type: ignore[assignment]
    item.updated_at = datetime.utcnow()  # type: ignore[assignment]
    db.commit()
    inc("jobhunter_queue_paused_total", pipeline=item.pipeline)
    log.warning("queue item %s paused (AI transient outage), re-queue in %ss: %s",
                item.id, delay, item.error)
    return "paused"


def drain_paused(db: Session, *, user_id: Optional[int] = None,
                 pipelines: Optional[Sequence[str]] = None, limit: int = 100,
                 force: bool = False) -> int:
    """Flip paused items back to ``queued`` now that AI is available.

    Called by the watchdog on every green probe (honouring the pause backoff)
    and by the one-click resume endpoint (``force=True`` — the user asked, so
    no waiting). Idempotent: only items currently in ``paused`` are touched.
    """
    query = db.query(PipelineJob).filter(PipelineJob.status == "paused")
    if not force:
        query = query.filter(PipelineJob.scheduled_at <= datetime.utcnow())
    if user_id is not None:
        query = query.filter(PipelineJob.user_id == user_id)
    if pipelines:
        query = query.filter(PipelineJob.pipeline.in_(list(pipelines)))
    items = query.order_by(PipelineJob.priority.asc(), PipelineJob.id.asc()).limit(limit).all()
    now = datetime.utcnow()
    for item in items:
        item.status = "queued"  # type: ignore[assignment]
        item.scheduled_at = now  # type: ignore[assignment]
        item.updated_at = now  # type: ignore[assignment]
    if items:
        db.commit()
        inc("jobhunter_queue_resumed_total", value=len(items))
        log.info("resumed %s paused queue item(s) — AI is back", len(items))
    return len(items)


def paused_count(db: Session, *, user_id: Optional[int] = None) -> int:
    query = db.query(func.count(PipelineJob.id)).filter(PipelineJob.status == "paused")
    if user_id is not None:
        query = query.filter(PipelineJob.user_id == user_id)
    return int(query.scalar() or 0)


def fail(db: Session, item: PipelineJob, error: str, *, retryable: bool = True) -> str:
    """Record a handler failure; retry with backoff or move to the dead-letter state.

    This is the **only** place ``attempts`` grows: it counts failures, and the
    increment happens *before* the budget check, so ``max_attempts=3`` means
    three recorded failures and the third one is the one that dead-letters
    (``attempts >= max_attempts`` → ``dead``). Claims, pauses and lease
    expiries never touch it — see the module docstring.
    """
    item.error = (error or "unknown error")[:2000]
    item.locked_by = ""
    item.lease_expires_at = None
    item.attempts = int(item.attempts or 0) + 1  # type: ignore[assignment]
    if retryable and item.attempts < (item.max_attempts or settings.worker_max_attempts):
        # Backoff is keyed on the failure just recorded, so the progression
        # is unchanged from the caller's point of view: first failure →
        # ``base ** 1 * 5``s, second → ``base ** 2 * 5``s, … capped at 600s.
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


def needs_input(db: Session, item: PipelineJob, *, reason: str = "",
                result: Optional[Dict[str, Any]] = None) -> None:
    """Park the item until the user answers.

    ``result`` (v2.2.8) is the handler's return value — the prepared plan with
    ``missing_fields`` and ``input_request_id``. It is stored like a completed
    run's result so the live layer can say *which* fields are missing on any
    route and after a refresh, instead of only "needs input".
    """
    item.status = "needs_input"
    item.error = reason[:2000]
    item.locked_by = ""
    item.lease_expires_at = None
    if result is not None:
        item.payload = {**(item.payload or {}), "result": result}
    db.commit()


def recover_stalled(db: Session, *, pipelines: Optional[Sequence[str]] = None) -> int:
    """Re-queue items whose lease expired (worker crash / deploy).

    A stalled item is one nobody ever called :func:`fail` for, so the decision
    is made on its *failure* count: ``attempts >= max_attempts`` → dead-letter
    (its budget was already spent by real failures — this is also what stops a
    row written before v2.2.2, whose counter was inflated by claims, from
    looping forever), otherwise re-queue and let it run again. Recovering a
    lease never increments ``attempts`` itself.
    """
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
        pipeline: {"queued": 0, "processing": 0, "done": 0, "failed": 0, "needs_input": 0, "dead": 0, "paused": 0}
        for pipeline in PIPELINES
    }
    for pipeline, status, count in rows:
        bucket = stats.setdefault(pipeline, {"queued": 0, "processing": 0, "done": 0, "failed": 0, "needs_input": 0, "dead": 0, "paused": 0})
        bucket[status] = int(count)
    set_gauge("jobhunter_queue_depth", sum(b.get("queued", 0) for b in stats.values()), scope="global")
    return stats


#: In-flight states that belong in the AI queue view. The AI pipeline IS the
#: durable queue (v2.1.1 removed the old in-process priority deque) — real
#: rows only, nothing synthesized.
AI_QUEUE_STATUSES = ("queued", "processing", "paused", "needs_input")


def ai_queue_view(db: Session, *, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Live view of the AI queue, built exclusively from durable rows.

    The Queues page renders two panels from this block:

    * ``queue_preview`` — up to 3 in-flight items with ``pipeline='ai'``,
      ordered by priority then arrival (``queued|processing|paused|needs_input``);
    * ``processing_now`` — the claimed item, if any (``null`` when idle).
    """

    def workflow_of(row: PipelineJob) -> str:
        payload: Dict[str, Any] = row.payload if isinstance(row.payload, dict) else {}
        task = payload.get("task")
        return str(task) if task else "ai"

    def ai_rows():
        query = db.query(PipelineJob).filter(PipelineJob.pipeline == "ai")
        if user_id is not None:
            query = query.filter(PipelineJob.user_id == user_id)
        return query

    preview = [
        {"id": row.id, "workflow": workflow_of(row), "priority": row.priority, "status": row.status}
        for row in ai_rows()
        .filter(PipelineJob.status.in_(list(AI_QUEUE_STATUSES)))
        .order_by(PipelineJob.priority.asc(), PipelineJob.created_at.asc(), PipelineJob.id.asc())
        .limit(3)
    ]
    processing = (
        ai_rows()
        .filter(PipelineJob.status == "processing")
        .order_by(PipelineJob.id.asc())
        .first()
    )
    return {
        "queue_preview": preview,
        "processing_now": (
            {"id": processing.id, "workflow": workflow_of(processing)} if processing else None
        ),
    }


def recent_items(db: Session, *, user_id: int, pipeline: Optional[str] = None, status: Optional[str] = None,
                 limit: int = 50) -> List[PipelineJob]:
    """Newest rows for one tenant. ``status`` accepts a comma-separated list."""
    query = db.query(PipelineJob).filter(PipelineJob.user_id == user_id)
    if pipeline:
        query = query.filter(PipelineJob.pipeline == pipeline)
    if status:
        wanted = [s.strip() for s in str(status).split(",") if s.strip()]
        if len(wanted) == 1:
            query = query.filter(PipelineJob.status == wanted[0])
        elif wanted:
            query = query.filter(PipelineJob.status.in_(wanted))
    return query.order_by(PipelineJob.created_at.desc()).limit(limit).all()


def existing_item(db: Session, *, user_id: int, dedupe_key: str) -> Optional[PipelineJob]:
    """The tenant's row behind a dedupe key (``None`` when there is none).

    :func:`enqueue` answers ``None`` for "identical work is already in flight",
    which is the right *queue* answer but leaves the API without the id the UI
    needs to re-attach to that in-flight row. Routers call this to return the
    live ``pipeline_job_id`` on the duplicate path too.
    """
    if not dedupe_key:
        return None
    return (
        db.query(PipelineJob)
        .filter(PipelineJob.user_id == user_id, PipelineJob.dedupe_key == dedupe_key)
        .first()
    )


def enqueue_or_existing(db: Session, **kwargs: Any) -> tuple[Optional[PipelineJob], bool]:
    """:func:`enqueue`, but always hand back a row.

    Returns ``(item, duplicate)``: ``duplicate`` is ``True`` when the work was
    already in flight and ``item`` is that in-flight row (or ``None`` when no
    dedupe key was given, in which case nothing could have been deduped).
    """
    item = enqueue(db, **kwargs)
    if item is not None:
        return item, False
    return existing_item(db, user_id=int(kwargs["user_id"]), dedupe_key=str(kwargs.get("dedupe_key") or "")), True


#: Rows the live layer treats as "still moving" — every status in which the
#: worker or the watchdog may still change the row without user action, plus
#: ``needs_input`` (which needs the user, and must therefore stay visible).
LIVE_STATUSES = ("queued", "processing", "paused", "needs_input")

#: How long a finished row stays in the live feed so a page that was refreshed
#: mid-run still sees the *outcome* (done/dead) and can transition instead of
#: silently losing the status the user was watching.
RECENT_FINISHED_WINDOW = timedelta(minutes=30)


def serialize_item(row: PipelineJob, *, include_result: bool = True) -> Dict[str, Any]:
    """One ``pipeline_jobs`` row as the UI sees it.

    ``payload.result`` (the handler's return value) is what the pages derive
    their final state from — ``applied`` / ``ready_to_apply`` / ``needs_input``
    for an application, ``resume_id`` + download links for a resume, the scan
    report for funding. Everything the UI needs to render a run *without*
    re-clicking must be in here, because after an F5 this row is all it has.
    """
    payload: Dict[str, Any] = dict(row.payload) if isinstance(row.payload, dict) else {}
    result = payload.pop("result", None)
    # Discovery/funding carry large context blobs; the live feed does not need them.
    for heavy in ("context", "board_tokens"):
        payload.pop(heavy, None)
    out: Dict[str, Any] = {
        "id": row.id,
        "pipeline": row.pipeline,
        "task": payload.get("task"),
        "job_id": row.job_id,
        "status": row.status,
        "priority": row.priority,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "paused_count": int(payload.get("paused_count") or 0),
        "error": row.error or "",
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "scheduled_at": row.scheduled_at,
        "finished_at": row.finished_at,
        "payload": payload,
    }
    if include_result:
        out["result"] = result if isinstance(result, dict) else (None if result is None else {"result": result})
    return out


def live_view(db: Session, *, user_id: int, limit: int = 100) -> Dict[str, Any]:
    """Everything one tenant's live layer polls: in-flight rows + fresh outcomes.

    * ``items`` — every row in :data:`LIVE_STATUSES`, oldest first (the order
      the worker will get to them);
    * ``recent`` — rows finished (``done``/``dead``/``failed``) inside
      :data:`RECENT_FINISHED_WINDOW`, newest first, so a refreshed page can
      still show "applied" for a run it launched before the refresh;
    * ``counts`` — the header chip numbers;
    * ``processing_now`` — the row a worker is executing right now (or null).

    Tenant-scoped by construction: every query filters on ``user_id``.
    """
    base = db.query(PipelineJob).filter(PipelineJob.user_id == user_id)
    live_rows = (
        base.filter(PipelineJob.status.in_(list(LIVE_STATUSES)))
        .order_by(PipelineJob.priority.asc(), PipelineJob.created_at.asc(), PipelineJob.id.asc())
        .limit(limit)
        .all()
    )
    since = datetime.utcnow() - RECENT_FINISHED_WINDOW
    recent_rows = (
        base.filter(
            PipelineJob.status.in_(["done", "dead", "failed"]),
            or_(PipelineJob.finished_at >= since, PipelineJob.updated_at >= since),
        )
        .order_by(PipelineJob.id.desc())
        .limit(limit)
        .all()
    )
    counts = dict.fromkeys(LIVE_STATUSES, 0)
    for row in live_rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    processing = next((row for row in live_rows if row.status == "processing"), None)
    return {
        "counts": counts,
        "items": [serialize_item(row) for row in live_rows],
        "recent": [serialize_item(row) for row in recent_rows],
        "processing_now": serialize_item(processing) if processing else None,
        "server_time": datetime.utcnow(),
    }
