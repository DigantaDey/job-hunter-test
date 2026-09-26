"""
Durable, tenant-scoped work queue for the pipelines.

Why not ``asyncio.create_task`` in the request handler (the v1.2 approach)? A
rolling deploy, a crash or a restart silently loses in-flight work. This queue
persists every unit of work in ``pipeline_jobs`` and gives workers:

* atomic claim with a lease (``status=processing`` + ``lease_expires_at``);
* a **lease heartbeat** (:func:`renew`) — the worker extends its own lease while
  the handler runs, so :func:`recover_stalled` only ever recovers rows whose
  worker really stopped instead of cloning a merely slow one;
* an **ownership-fenced completion** (:func:`complete` is a compare-and-set on
  ``status``/``locked_by``, and :func:`owns` is the same probe for the failure
  paths), so an outcome can never overwrite the row a second worker now holds;
* **per-pipeline slot budgets** (:func:`pipeline_limits`, ``WORKER_PIPELINE_LIMITS``)
  so one minutes-long pipeline cannot occupy every worker slot;
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
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc, set_gauge
from app.models.models import PipelineJob

log = get_logger("app.queue")

#: ``extraction`` is the resumable onboarding resume-parse (see
#: :mod:`app.services.onboarding`) — the AI half of a resume upload, moved out
#: of the HTTP request so a refresh/restart never loses it.
PIPELINES = ("discovery", "application", "email", "funding", "ai", "extraction",
             "browser_session")


#: The process-wide worker identity, minted once (see :func:`worker_id`).
_PROCESS_WORKER_ID: Optional[str] = None


def worker_id() -> str:
    """This worker's stable identity, used as the lease owner (``locked_by``).

    **Stable for the life of the process** (v2.4). It used to mint a fresh
    ``host-pid-random`` string on every call, which was harmless only while
    nothing ever compared it: the lease was written at claim time and never read
    back. Now that the worker *renews* its own lease (:func:`renew`) and
    :func:`complete` is a compare-and-set on ``locked_by``, a per-call identity
    would have every claim immediately refuse its own heartbeat — the worker
    would fence itself out of its own row. ``WORKER_ID`` still wins when an
    operator sets it (one identity per container, which is what makes the value
    readable in ``pipeline_jobs.locked_by``).
    """
    global _PROCESS_WORKER_ID
    if settings.worker_id:
        return settings.worker_id
    if _PROCESS_WORKER_ID is None:
        _PROCESS_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    return _PROCESS_WORKER_ID


def _note_dedupe(scope: str) -> None:
    """Count one duplicate the queue refused to run twice.

    ``scope`` is always a literal written at the call site (never request data),
    so the label set stays bounded without a lookup table.
    """
    inc("jobhunter_dedupe_hits_total", scope=scope)


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
                # Duplicate work refused. Counted, not silent: a dedupe key that
                # stops matching looks exactly like a quiet week right up until
                # the second application is submitted.
                _note_dedupe("queue")
                return None  # identical work is already in flight (paused = waiting on AI)
            existing.pipeline = pipeline
            existing.job_id = job_id
            existing.payload = payload or {}
            existing.status = "queued"
            existing.priority = max(1, min(9, priority))
            existing.attempts = 0
            existing.reclaim_count = 0
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
        _note_dedupe("queue")
        return (
            db.query(PipelineJob)
            .filter(PipelineJob.user_id == user_id, PipelineJob.dedupe_key == dedupe_key)
            .first()
        )
    db.refresh(item)
    inc("jobhunter_queue_enqueued_total", pipeline=pipeline)
    return item


#: Parsed ``WORKER_PIPELINE_LIMITS`` keyed by the raw setting, so an operator's
#: string is parsed once per value (and the "unknown pipeline" warning is logged
#: once per value too, not once per claim).
_PIPELINE_LIMITS_CACHE: Dict[str, Dict[str, int]] = {}


def pipeline_limits(configured: Optional[Mapping[str, int]] = None) -> Dict[str, int]:
    """Per-pipeline slot budgets: ``{"discovery": 1, ...}``.

    ``WORKER_PIPELINE_LIMITS`` is a comma-separated ``pipeline=count`` list
    (``"discovery=1,application=2"``). ``configured`` overrides the setting for
    one call — that is what tests and a standalone worker use.

    A budget is the maximum number of rows of that pipeline that may be
    ``processing`` at once **across the deployment**, not per worker: that is
    what stops a minutes-long discovery run from holding half the platform's
    job slots (``WORKER_CONCURRENCY``) while everyone else's email/application
    work waits behind it. ``0`` (or a negative count) means *no budget* for
    that pipeline, and an unlisted pipeline has none either — the pre-v2.4
    behaviour, exactly.

    Unknown pipeline names are ignored (with one warning per distinct setting):
    a typo must not silently cap nothing, and it must not raise inside a claim.
    """
    if configured is not None:
        return {
            str(name): int(count)
            for name, count in configured.items()
            if str(name) in PIPELINES and int(count or 0) > 0
        }
    raw = str(getattr(settings, "worker_pipeline_limits", "") or "").strip()
    if not raw:
        return {}
    cached = _PIPELINE_LIMITS_CACHE.get(raw)
    if cached is not None:
        return dict(cached)
    parsed: Dict[str, int] = {}
    unknown: List[str] = []
    for token in raw.split(","):
        name, _, value = token.partition("=")
        name = name.strip()
        if not name:
            continue
        if name not in PIPELINES:
            unknown.append(name)
            continue
        try:
            count = int((value or "").strip())
        except ValueError:
            unknown.append(name)
            continue
        if count > 0:
            parsed[name] = count
    if unknown:
        log.warning("WORKER_PIPELINE_LIMITS ignores %s (not a pipeline and/or not a count): %s",
                    ", ".join(sorted(set(unknown))), raw)
    _PIPELINE_LIMITS_CACHE[raw] = dict(parsed)
    return parsed


def _pipelines_at_capacity(
    db: Session,
    *,
    pipelines: Sequence[str],
    limits: Mapping[str, int],
) -> List[str]:
    """Pipelines in ``pipelines`` whose processing rows already fill their budget.

    One grouped ``COUNT`` over indexed columns; skipped entirely (no query at
    all) when no limit applies to the requested pipelines, so a deployment that
    configures none pays exactly what it paid before this existed.
    """
    scoped = [p for p in pipelines if int(limits.get(p, 0) or 0) > 0]
    if not scoped:
        return []
    now = datetime.utcnow()
    rows = (
        db.query(PipelineJob.pipeline, func.count(PipelineJob.id))
        .filter(
            PipelineJob.status == "processing",
            PipelineJob.pipeline.in_(scoped),
            # Only *live* leases occupy a slot: a row whose lease has expired is
            # not being served (its worker died, or its heartbeat is failing),
            # the reaper is about to re-queue it, and letting it hold the budget
            # would stall the whole pipeline for the lease + safety margin. A
            # live worker renews its lease every ``WORKER_HEARTBEAT_INTERVAL_
            # SECONDS``, so a healthy run never looks expired.
            or_(PipelineJob.lease_expires_at.is_(None), PipelineJob.lease_expires_at > now),
        )
        .group_by(PipelineJob.pipeline)
        .all()
    )
    counts: Dict[str, int] = {str(pipeline): int(count or 0) for pipeline, count in rows}
    return [p for p in scoped if int(counts.get(p, 0) or 0) >= int(limits[p])]


def claim(db: Session, *, pipelines: Sequence[str], lease_seconds: Optional[int] = None,
          limits: Optional[Mapping[str, int]] = None) -> Optional[PipelineJob]:
    """Atomically claim the next runnable item (priority, then arrival order).

    Claiming takes a lease only — it does **not** increment ``attempts``,
    which counts handler failures (see :func:`fail` and the module
    docstring). A pause/resume or crash/re-queue cycle therefore costs the
    item nothing.

    ``limits`` is the per-pipeline slot budget (:func:`pipeline_limits`,
    defaulting to the ``WORKER_PIPELINE_LIMITS`` setting): a pipeline whose
    processing rows already fill its budget is filtered out of the *query*, so
    the next claim is the highest-priority runnable item of a pipeline that has
    a free slot — a blocked discovery queue never starves the email queue
    behind it, and a claim that finds nothing returns ``None`` instead of
    overshooting the budget.
    """
    now = datetime.utcnow()
    lease = timedelta(seconds=lease_seconds or settings.worker_lease_seconds)
    scope = [p for p in pipelines if p in PIPELINES] or list(pipelines)
    effective_limits = pipeline_limits(limits)
    blocked = _pipelines_at_capacity(db, pipelines=scope, limits=effective_limits)
    runnable = [p for p in scope if p not in blocked]
    if not runnable:
        return None
    candidate_id = (
        db.query(PipelineJob.id)
        .filter(
            PipelineJob.pipeline.in_(runnable),
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


def renew(db: Session, item: Any, *, worker: Optional[str] = None,
          lease_seconds: Optional[int] = None) -> bool:
    """Extend the lease of a row this worker still owns — the v2.4 heartbeat.

    One conditional ``UPDATE``: it only lands while the row is still
    ``processing`` **and** still locked by this worker, and the rowcount is the
    answer. That is what makes renewing safe to interleave with the reaper: if
    the lease was lost (the reaper re-queued the row, another worker claimed it),
    the update changes nothing and reports ``False`` — the caller stops renewing
    and its eventual outcome is discarded by the compare-and-set in
    :func:`complete`.

    Without this, ``WORKER_LEASE_SECONDS`` was a hard lie for long work: the
    lease was written once at claim time and never refreshed, so any run longer
    than ``lease + WORKER_REAPER_SAFETY_SECONDS`` (120 + 300 s out of the box)
    was re-queued *while its first copy was still running* — the same sources
    fetched twice, the same AI verdicts paid for twice, on exactly the runs that
    were already slow. Returns ``True`` when the lease was extended.

    ``item`` is the claimed row or its id — the heartbeat runs in its *own*
    session (see ``worker._heartbeat_lease``), where the row object belongs to the
    handler's session, so the identity is read from the object and the update is
    addressed by primary key.
    """
    now = datetime.utcnow()
    item_id = int(item.id) if isinstance(item, PipelineJob) else int(item)
    pipeline = str(getattr(item, "pipeline", "") or "")
    owner = str(worker or getattr(item, "locked_by", "") or worker_id())
    lease = timedelta(seconds=lease_seconds or settings.worker_lease_seconds)
    updated = (
        db.query(PipelineJob)
        .filter(
            PipelineJob.id == item_id,
            PipelineJob.status == "processing",
            PipelineJob.locked_by == owner,
        )
        .update(
            {
                PipelineJob.lease_expires_at: now + lease,
                PipelineJob.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not updated:
        return False
    # Keep the in-session row honest about its own lease (the UPDATE bypassed the
    # identity map) — but only when it really lives in this session; the
    # heartbeat's own session must not touch the handler's object.
    if isinstance(item, PipelineJob) and object_session(item) is db:
        db.refresh(item, attribute_names=["lease_expires_at", "updated_at"])
    inc("jobhunter_queue_lease_renewed_total", pipeline=pipeline)
    return True


def owns(db: Session, item: PipelineJob, *, worker: Optional[str] = None) -> bool:
    """Is this row still *this worker's* in-flight item?

    A cheap, read-only ownership probe (two columns by primary key) for the
    transitions that have no compare-and-set of their own: the failure record
    (:func:`fail`), a park (:func:`needs_input`) and a pause. A worker whose
    lease was reclaimed while the handler ran must not write *any* of them over
    the state the new owner is writing — losing a slow run is acceptable, two
    workers editing one row's outcome is not.

    ``worker`` defaults to the row's own ``locked_by`` (the identity the claim
    wrote), which is why a row claimed by another process — the reaper's winner,
    or a pre-v2.4 row — is still answerable.
    """
    owner = str(worker or item.locked_by or worker_id())
    row = (
        db.query(PipelineJob.status, PipelineJob.locked_by)
        .filter(PipelineJob.id == item.id)
        .first()
    )
    if row is None:
        return False
    status, locked_by = row[0], str(row[1] or "")
    return status == "processing" and locked_by == owner


def complete(db: Session, item: PipelineJob, *, result: Optional[Dict[str, Any]] = None,
             worker: Optional[str] = None) -> bool:
    """Finish a run — as a compare-and-set on the row's ownership.

    The outcome is only written while the row is still ``processing`` and still
    locked by the identity that produced it. ``False`` means this worker no
    longer owns the row (the reaper re-queued it, or a second worker already
    finished it) and **nothing was written**: the run's result is discarded
    instead of overwriting whatever the current owner wrote. Before v2.4 the
    write was unconditional — two copies of one run both finished, and the last
    writer's ``payload.result`` won, silently.

    ``worker`` is the identity that must still hold the row. The worker passes
    its own id explicitly: after a handler commits, ``item.locked_by`` has been
    re-read from the database and would otherwise describe whoever owns the row
    *now* — the fence has to compare against the identity that ran the work, not
    against a value the reclaim may have already overwritten.

    Returns ``True`` when this call is the one that completed the row.
    """
    now = datetime.utcnow()
    owner = str(worker or item.locked_by or worker_id())
    values: Dict[Any, Any] = {
        PipelineJob.status: "done",
        PipelineJob.finished_at: now,
        PipelineJob.lease_expires_at: None,
        PipelineJob.locked_by: "",
        PipelineJob.error: "",
        PipelineJob.updated_at: now,
    }
    if result is not None:
        values[PipelineJob.payload] = {**(item.payload or {}), "result": result}
    updated = (
        db.query(PipelineJob)
        .filter(
            PipelineJob.id == item.id,
            PipelineJob.status == "processing",
            PipelineJob.locked_by == owner,
        )
        .update(values, synchronize_session=False)
    )
    db.commit()
    if not updated:
        log.warning("queue item %s (%s) no longer held by this worker — outcome discarded "
                    "(the row was reclaimed and is owned elsewhere)", item.id, item.pipeline)
        inc("jobhunter_queue_complete_conflicts_total", pipeline=item.pipeline)
        return False
    # Keep the in-session row honest (the UPDATE bypassed the identity map) so
    # callers that read ``item.status`` / ``item.payload`` after completing see
    # the row they just wrote.
    db.refresh(item)
    inc("jobhunter_queue_completed_total", pipeline=item.pipeline)
    return True


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
    item.error = (error or "ai_transient_outage")[:2000]
    item.locked_by = ""
    item.lease_expires_at = None
    payload = dict(item.payload or {})
    pauses = int(payload.get("paused_count") or 0) + 1
    if pauses > AI_PAUSE_MAX:
        item.status = "dead"
        item.finished_at = datetime.utcnow()
        item.error = f"paused {AI_PAUSE_MAX} times during AI outages, giving up: {item.error}"
        db.commit()
        inc("jobhunter_queue_dead_total", pipeline=item.pipeline)
        log.error("queue item %s dead-lettered after repeated AI outages", item.id)
        return "dead"
    payload["paused_count"] = pauses
    delay = min(600, int(settings.ai_backoff_base ** min(pauses, 8) * 5))
    item.payload = payload
    item.status = "paused"
    item.scheduled_at = datetime.utcnow() + timedelta(seconds=delay)
    item.updated_at = datetime.utcnow()
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
        item.status = "queued"
        item.scheduled_at = now
        item.updated_at = now
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
    item.attempts = int(item.attempts or 0) + 1
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

    A parked item is **never retried** and it does not sit there forever: the
    expiry deadline is written into the row here, and the worker's user-action
    sweep (:func:`app.services.reliability.expire_user_actions`) closes the row
    with a reason once that deadline passes. Storing the deadline in the row —
    rather than computing it from "now + TTL" at sweep time — is what makes a
    restart, a redeploy and a second worker agree on the same moment.
    """
    item.status = "needs_input"
    item.error = reason[:2000]
    item.locked_by = ""
    item.lease_expires_at = None
    payload = dict(item.payload or {})
    if result is not None:
        payload["result"] = result
    moment = datetime.utcnow()
    action = dict(payload.get("user_action") or {})
    action.setdefault("since", moment.isoformat(timespec="seconds") + "Z")
    action["kind"] = str((result or {}).get("action_kind") or action.get("kind")
                         or "review_required")
    if not action.get("expires_at"):
        action["expires_at"] = (
            moment + timedelta(seconds=_user_action_ttl_seconds())
        ).isoformat(timespec="seconds") + "Z"
    payload["user_action"] = action
    item.payload = payload
    db.commit()
    inc("jobhunter_queue_needs_input_total", pipeline=item.pipeline)


def _user_action_ttl_seconds() -> int:
    """The configured wait window, with a floor so a mis-set env cannot expire instantly."""
    return max(60, int(getattr(settings, "user_action_ttl_minutes", 10080)) * 60)


def close_expired(db: Session, item: PipelineJob, error: str) -> str:
    """
    Finish a row whose user-action window elapsed.

    Deliberately **not** :func:`fail`: ``attempts`` counts handler failures, and
    a user who did not answer in time is not the work breaking. Expiry is a
    decision — terminal, visible, and never a retry — so it writes the terminal
    state directly and leaves every budget exactly as it found it.
    """
    item.status = "dead"
    item.error = (error or "user_action_expired")[:2000]
    item.finished_at = datetime.utcnow()
    item.lease_expires_at = None
    item.locked_by = ""
    item.updated_at = datetime.utcnow()
    db.commit()
    inc("jobhunter_queue_dead_total", pipeline=item.pipeline)
    log.info("queue item %s closed: %s", item.id, item.error)
    return "dead"


def recover_stalled(
    db: Session,
    *,
    pipelines: Optional[Sequence[str]] = None,
    safety_margin_seconds: Optional[float] = None,
    max_reclaims: Optional[int] = None,
) -> int:
    """Re-queue items whose lease expired (worker crash / deploy) with CAS safety.

    This is the **periodic lease reaper** (v2.3). Earlier versions ran only at
    worker startup and did a read-modify-write without a compare-and-set, so:

    * a slow/hung AI call (lease 120s, AI timeout 300s) left the job stuck
      ``processing`` for the rest of the process lifetime;
    * on a rolling deploy the new worker's startup sweep could re-queue a row
      while the original process was still running it — duplicate outreach,
      duplicate applications, double AI spend.

    The new contract:

    * **Periodic**: called every :data:`worker_reaper_interval_seconds` from
      each worker's poll loop, not only at boot.
    * **CAS**: re-claim is a single ``UPDATE ... WHERE id=:id AND
      status='processing' AND lease_expires_at < cutoff`` — exactly one
      concurrent reclaimer wins (rowcount check).
    * **Safety margin**: only re-queue when the lease has been expired by more
      than the maximum single-job duration (default 300s = AI timeout), so a
      merely-slow job inside a 300s AI call is not cloned under a live worker.
    * **Crash-loop guard**: ``reclaim_count`` (how many times the row was
      re-queued due to lease expiry) is tracked atomically; after N reclaims
      the row is moved to dead-letter instead of bouncing forever.
    * **Observability**: per re-claimed job log + metric (id, pipeline, age,
      claim count).

    A stalled item is one nobody ever called :func:`fail` for, so the decision
    is made on its *failure* count (``attempts >= max_attempts`` → dead) and
    on its *reclaim* count (``reclaim_count >= max_reclaims`` → dead).
    Recovering a lease never increments ``attempts`` itself.
    """
    now = datetime.utcnow()
    # Safety margin: only consider leases expired by more than this.
    # Default from settings (300s = ai_timeout) to prevent cloning a live
    # worker still inside a 300s AI call. Tests that want immediate recovery
    # pass safety_margin_seconds=0.
    if safety_margin_seconds is None:
        safety_margin_seconds = float(
            getattr(settings, "worker_reaper_safety_seconds", 300.0)
        )
    safety_margin_seconds = max(0.0, float(safety_margin_seconds))
    cutoff = now - timedelta(seconds=safety_margin_seconds)

    if max_reclaims is None:
        max_reclaims = int(
            getattr(settings, "worker_max_reclaims", 0)
            or getattr(settings, "worker_max_attempts", 3)
        )
    max_reclaims = max(1, int(max_reclaims))

    # Candidates: processing rows whose lease is None (very old) or expired
    # beyond the safety margin.
    query = db.query(PipelineJob).filter(
        PipelineJob.status == "processing",
        or_(PipelineJob.lease_expires_at.is_(None), PipelineJob.lease_expires_at < cutoff),
    )
    if pipelines:
        query = query.filter(PipelineJob.pipeline.in_(list(pipelines)))
    stalled = query.all()

    recovered = 0
    for item in stalled:
        attempts = int(item.attempts or 0)
        max_attempts = int(item.max_attempts or settings.worker_max_attempts)
        reclaim_count = int(getattr(item, "reclaim_count", 0) or 0)

        # Age for observability: how long has the lease been expired?
        age_seconds = 0.0
        if item.lease_expires_at:
            try:
                age_seconds = (now - item.lease_expires_at).total_seconds()
            except Exception:
                age_seconds = safety_margin_seconds
        else:
            # No lease timestamp — treat as very old, use updated_at.
            try:
                base = item.updated_at or item.created_at or now
                age_seconds = (now - base).total_seconds()
            except Exception:
                age_seconds = safety_margin_seconds

        should_dead = False
        dead_reason = ""
        if attempts >= max_attempts:
            should_dead = True
            dead_reason = item.error or "lease expired"
        elif reclaim_count >= max_reclaims:
            should_dead = True
            dead_reason = (
                f"crash-looped {reclaim_count} times, giving up: "
                f"{item.error or 'lease expired'}"
            )

        # CAS update: only succeed if row is still processing and lease still expired.
        # This makes racing reclaimers safe — exactly one wins (rowcount).
        base_filter = [
            PipelineJob.id == item.id,
            PipelineJob.status == "processing",
            or_(
                PipelineJob.lease_expires_at.is_(None),
                PipelineJob.lease_expires_at < cutoff,
            ),
        ]

        if should_dead:
            updated = (
                db.query(PipelineJob)
                .filter(*base_filter)
                .update(
                    {
                        PipelineJob.status: "dead",
                        PipelineJob.finished_at: now,
                        PipelineJob.error: (dead_reason or "lease expired")[:2000],
                        PipelineJob.locked_by: "",
                        PipelineJob.lease_expires_at: None,
                        PipelineJob.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if updated:
                recovered += 1
                log.warning(
                    "reclaimed stalled job id=%s pipeline=%s age=%.1fs "
                    "reclaim_count=%s attempts=%s/%s -> dead (%s)",
                    item.id,
                    item.pipeline,
                    age_seconds,
                    reclaim_count,
                    attempts,
                    max_attempts,
                    (dead_reason or "lease expired")[:200],
                )
                inc("jobhunter_queue_recovered_total", value=1)
                inc(
                    "jobhunter_queue_reclaimed_total",
                    pipeline=item.pipeline,
                    outcome="dead",
                )
                inc("jobhunter_queue_reclaim_total", pipeline=item.pipeline)
        else:
            # Re-queue: atomic increment of reclaim_count via SQL expression.
            # coalesce handles legacy rows where the column is NULL.
            updated = (
                db.query(PipelineJob)
                .filter(*base_filter)
                .update(
                    {
                        PipelineJob.status: "queued",
                        PipelineJob.scheduled_at: now,
                        PipelineJob.locked_by: "",
                        PipelineJob.lease_expires_at: None,
                        PipelineJob.updated_at: now,
                        PipelineJob.reclaim_count: func.coalesce(
                            PipelineJob.reclaim_count, 0
                        )
                        + 1,
                    },
                    synchronize_session=False,
                )
            )
            if updated:
                recovered += 1
                new_count = reclaim_count + 1
                log.warning(
                    "reclaimed stalled job id=%s pipeline=%s age=%.1fs "
                    "reclaim_count=%s attempts=%s/%s -> queued",
                    item.id,
                    item.pipeline,
                    age_seconds,
                    new_count,
                    attempts,
                    max_attempts,
                )
                inc("jobhunter_queue_recovered_total", value=1)
                inc(
                    "jobhunter_queue_reclaimed_total",
                    pipeline=item.pipeline,
                    outcome="requeued",
                )
                inc("jobhunter_queue_reclaim_total", pipeline=item.pipeline)
                # Per-pipeline, never per-job: a series per reclaimed row is a
                # series per incident, and the registry keeps series for the
                # life of the process. The row's own id, age and budget are in
                # the log line above — that is where an operator looks for one
                # specific job.
                set_gauge(
                    "jobhunter_queue_reclaim_age_seconds",
                    age_seconds,
                    pipeline=item.pipeline,
                )

    if recovered:
        db.commit()
        log.warning("recovered %s stalled queue item(s)", recovered)
    return recovered


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
        "reclaim_count": int(getattr(row, "reclaim_count", 0) or 0),
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
