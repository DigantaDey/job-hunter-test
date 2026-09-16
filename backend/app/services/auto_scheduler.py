"""
Auto-mode scheduler — the thing that makes "premium automation" real.

Until v2.2 the promise was a phantom: ``can_use_scheduled_workflows`` was True
for Pro/Pro+ but nothing consumed it, ``automation_runs_per_month`` existed as a
quota but nothing incremented it, and the Dashboard's used/limit readout was
wired to that dead counter. The system only moved when a user clicked.

This module is the missing piece, and it is deliberately small:

* it **only decides when to enqueue**. Every unit of work is a normal
  ``pipeline_jobs`` row executed by the existing handlers, so claim/retry/
  pause/dead-letter semantics, per-user AI key resolution and the v2.1 pause
  contract apply to auto work exactly as they do to a manual click;
* it is **a child task of the worker** (spawned in ``Worker.start``), which
  already supervises its children: a crash is logged with its traceback, counted
  and respawned with capped backoff. No second supervisor lives here;
* it is **idempotent per cadence window**. The window a decision belongs to is
  ``cycle_bucket`` (epoch seconds // cadence); it is carried into the queue's
  ``dedupe_key`` *and* into ``scheduled_runs.cycle_bucket``, so two sweeps inside
  one window enqueue exactly one item — even across worker processes;
* it is **per-user isolated**. Every user gets a fresh session and a try/except,
  so one corrupt settings row cannot stop anyone else's sweep;
* it **never enqueues into a known AI outage**. ``ai_availability`` is a
  60s-cached probe, so checking is cheap; the v2.1 pause contract still covers an
  outage that starts *mid-run*, but enqueueing work we already know will pause is
  churn, quota burn and a worse queue for everyone else.

Skipping is a first-class outcome, not a silent one: every ``skipped_*`` state is
written to ``scheduled_runs`` with a reason, which is what the Settings card and
``GET /api/automation`` render. Automation that quietly does nothing is
indistinguishable from a broken install.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.entitlements import can as capability_can
from app.core.entitlements import check_limit, current_period, expire_subscriptions, get_user_plan, usage_for
from app.core.logging import get_logger
from app.core.metrics import inc
from app.db import SessionLocal
from app.models.models import Job, Notification, PipelineJob, ScheduledRun, SettingsModel, User
from app.services.ai_client import STATE_ONLINE, ai_availability
from app.services.job_queue import enqueue
from app.services.user_settings import coerce_bool, get_setting

log = get_logger("app.auto_scheduler")

# --------------------------------------------------------------------------- #
# Cadence — THE table that decides how often auto mode runs.
#
# A schedule is a promise to the user, so it is written once, here, and read by
# every surface that shows or enforces it: the sweep, the Settings card, the
# dashboard's next-run line and the entitlement/settings payload. Small and boring
# on purpose — one number per (plan, workflow), in seconds:
#
#   pro_plus   discovery every 2 h  (12 scans/day)
#              funding  every 6 h   (4 scans/day)
#              application-prep every 24 h (1 pass/day)
#   pro        discovery every 12 h (2 scans/day)
#              funding  every 24 h  (1 scan/day)
#   free       no entry at all — and the settings gate refuses
#              ``auto_mode=true`` without the capability, so the row cannot even
#              be written by a free account.
#
# Deliberately absent: application-prep on ``pro``. A prepared application spends
# the tailored-resume budget; a daily automated pass is the tier that pays for
# it, so Pro keeps the scanning (discovery + funding radar) and prepares on
# demand. Adding a row here is the whole change when a cadence is added — nothing
# else in the product hardcodes a schedule.
# --------------------------------------------------------------------------- #
DISCOVERY = "discovery"
FUNDING = "funding"
APPLICATION_PREP = "application_prep"

CADENCE_SECONDS: Dict[str, Dict[str, int]] = {
    "pro": {DISCOVERY: 12 * 3600, FUNDING: 24 * 3600},
    "pro_plus": {DISCOVERY: 2 * 3600, FUNDING: 6 * 3600, APPLICATION_PREP: 24 * 3600},
}

#: The monthly run budget the Dashboard counts, and the one the worker charges
#: when an auto item completes (see ``Worker._run_item``).
AUTOMATION_LIMIT = "automation_runs_per_month"

#: auto workflow → the durable-queue pipeline that executes it. The handlers are
#: reused unchanged; auto mode adds a ``trigger: "auto"`` marker to the payload,
#: which is how the worker knows to charge the automation quota and how the
#: handlers know to notify about the run's findings.
PIPELINE_FOR_WORKFLOW: Dict[str, str] = {
    DISCOVERY: "discovery",
    FUNDING: "funding",
    APPLICATION_PREP: "application",
}

#: auto workflow → the ``workflows`` settings toggle that must be on for it.
#: There is no dedicated funding/interview toggle in the settings surface, so
#: auto mode reuses the closest one rather than inventing a switch the user
#: cannot find anywhere else: the radar is AI discovery over funding events, and
#: application-prep is resume tailoring. A workflow the user switched off is
#: never scheduled — auto mode cannot override a workflows toggle.
SETTING_FOR_WORKFLOW: Dict[str, str] = {
    DISCOVERY: "ai_for_discovery",
    FUNDING: "ai_for_discovery",
    APPLICATION_PREP: "ai_for_resume",
}

#: Per-action limits a manual trigger enforces in its router *before* enqueuing
#: (``POST /jobs/discover``, ``POST /funding/refresh``, ``POST /jobs/{id}/apply``).
#: The queued handlers do not check them, so auto mode has to — otherwise a
#: schedule would be a way around a limit the same click is held to.
EXTRA_LIMITS_FOR_WORKFLOW: Dict[str, Tuple[str, ...]] = {
    DISCOVERY: ("jobs_discovered_per_month", "jobs_discovered_per_day"),
    FUNDING: ("funding_companies_per_month",),
    APPLICATION_PREP: ("applications_per_month",),
}

#: Auto work is background fill: it queues *behind* every interactive trigger
#: (discovery 3, funding 5, application 1) so a scheduled scan can never delay a
#: user who is sitting in front of the app.
AUTO_PRIORITY = 6

#: Jobs one auto application-prep pass touches. One per pass, highest score
#: first — the next pass takes the next job. A scheduler that prepared the whole
#: backlog in one sweep would spend the month's resume budget in minutes.
AUTO_APPLICATION_PER_PASS = 1

#: Cap on how many postings a single auto discovery pass ranks (the manual
#: trigger's own default): auto mode is not a cheaper way to run a bigger scan.
AUTO_DISCOVERY_LIMIT = 40

# --------------------------------------------------------------------------- #
# ScheduledRun states
# --------------------------------------------------------------------------- #
STATE_QUEUED = "queued"
STATE_DONE = "done"
STATE_PAUSED = "paused"
STATE_FAILED = "failed"
#: The run reached work that is waiting on the user (an application whose form
#: has required fields nobody else can answer). Not a failure, not "done" — but
#: it does close the window: the cadence clock moves on and auto mode never
#: re-queues work that is parked behind a human.
STATE_NEEDS_INPUT = "needs_input"
STATE_SKIPPED_OUTAGE = "skipped_outage"
STATE_SKIPPED_QUOTA = "skipped_quota"
STATE_SKIPPED_NO_CONSENT = "skipped_no_consent"

#: States that mean "this window's work is finished" — the clock the due check
#: reads (the contract: the *last completed-or-paused* auto run).
FINISHED_STATES: Tuple[str, ...] = (STATE_DONE, STATE_PAUSED, STATE_FAILED, STATE_NEEDS_INPUT)
#: States that record a decision *not* to enqueue. They are retryable: a window
#: skipped because the provider was down must still run once the provider is
#: back, even if that happens inside the same window.
SKIP_STATES: Tuple[str, ...] = (STATE_SKIPPED_OUTAGE, STATE_SKIPPED_QUOTA, STATE_SKIPPED_NO_CONSENT)
ALL_STATES: Tuple[str, ...] = (STATE_QUEUED, *FINISHED_STATES, *SKIP_STATES)

#: In-app notification kind for an exhausted automation budget (snake_case, like
#: every other ``Notification.kind``; nothing here sends email or SMS).
QUOTA_NOTIFICATION_KIND = "quota_exhausted"
#: Notification kinds the *handlers* emit for auto runs only (v2.2) — named here
#: because this module owns the vocabulary the Settings card and Queues render.
HIGH_MATCH_NOTIFICATION_KIND = "high_match"
FUNDING_MATCH_NOTIFICATION_KIND = "funding_match"

#: Queue statuses that mean "a worker has not finished with this item yet".
OPEN_QUEUE_STATUSES = ("queued", "processing", "paused", "needs_input")


# --------------------------------------------------------------------------- #
# Pure helpers — imported by the settings surface, the automation router and the
# dashboard, so all three read the same table and the same clock.
# --------------------------------------------------------------------------- #
def plan_cadence(plan: Optional[str]) -> Dict[str, int]:
    """The plan's schedule in seconds per workflow (``{}`` = nothing is scheduled)."""
    return dict(CADENCE_SECONDS.get(str(plan or "")) or {})


def scheduler_enabled() -> bool:
    """``AUTO_SCHEDULER_INTERVAL_SECONDS=0`` turns auto mode off process-wide."""
    return float(settings.auto_scheduler_interval_seconds) > 0


def auto_mode_enabled(db: Session, user_id: int) -> bool:
    """The user's own switch, read exactly as the sweep reads it."""
    return coerce_bool(get_setting(db, user_id, "automation", "auto_mode", False)) is True


def workflow_enabled(db: Session, user_id: int, workflow: str) -> bool:
    """Is the ``workflows`` toggle behind this auto workflow on?"""
    key = SETTING_FOR_WORKFLOW.get(workflow)
    if not key:
        return False
    return coerce_bool(get_setting(db, user_id, "workflows", key, True)) is not False


def consent_accepted(user: Optional[User], kind: str = "automation") -> bool:
    """Consent storage, read without a request context.

    ``app.core.auth.require_consent`` is a FastAPI dependency over the same
    field; the scheduler has no request, so it reads the user row the exact same
    way — one storage shape, two readers.
    """
    if user is None:
        return False
    consents = dict(user.consents or {})
    return bool(consents.get(f"{kind}_accepted_at"))


def cycle_bucket(now: datetime, cadence_seconds: int) -> int:
    """Which cadence window ``now`` falls in (``epoch seconds // cadence``).

    Every timestamp in this schema is *naive UTC*, so the epoch is taken as if
    the value were UTC. ``datetime.timestamp()`` on a naive datetime interprets
    it in the machine's local timezone — on a non-UTC host that would shift every
    window (and with it the dedupe key) by the offset, silently.
    """
    epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
    return epoch // max(1, int(cadence_seconds))


def dedupe_key(workflow: str, user_id: int, bucket: int) -> str:
    """The queue dedupe key for one (user, workflow, window): one item per window."""
    return f"auto:{workflow}:{user_id}:{bucket}"


def run_clock(run: Optional[ScheduledRun]) -> Optional[datetime]:
    """A run's ``triggered_at`` as a real datetime (``None`` = treat it as never run).

    Narrowing here instead of reading the column at each call site is what keeps
    the clock's two definitions — "is it due" and "when next" — from ever
    disagreeing about what a missing timestamp means.
    """
    moment = run.triggered_at if run is not None else None
    return moment if isinstance(moment, datetime) else None


def is_due(last_finished: Optional[ScheduledRun], now: datetime, cadence_seconds: int) -> bool:
    """Is this workflow due? One definition, shared by the sweep and the UI.

    "Due" = the last completed-or-paused auto run is at least ``cadence_seconds``
    old (a window that never ran is always due). Both sides compute it here, so
    the Settings card can never promise a next-run time the scheduler disagrees
    with. The comparison is inclusive: a run exactly one cadence old *is* due.
    """
    moment = run_clock(last_finished)
    if moment is None:
        return True
    return (now - moment) >= timedelta(seconds=max(1, int(cadence_seconds)))


def next_run_at(last_finished: Optional[ScheduledRun], now: datetime, cadence_seconds: int) -> datetime:
    """Earliest moment the scheduler will enqueue this workflow again."""
    moment = run_clock(last_finished)
    if moment is None:
        return now  # never run → due on the next sweep
    return moment + timedelta(seconds=max(1, int(cadence_seconds)))


def iso_utc(moment: Any) -> Optional[str]:
    """Render a naive-UTC timestamp as an *explicit* UTC ISO-8601 string.

    Most of the API hands out naive datetimes (which a browser then reads as
    local time, shifting them by the visitor's offset). The automation block's
    whole job is "the next run happens at *this* moment", so an hour out would
    not be cosmetic — these fields carry the offset.
    """
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc).isoformat()
    return moment.astimezone(timezone.utc).isoformat()


def _as_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def users_with_auto_mode(db: Session) -> List[int]:
    """User ids whose ``automation.auto_mode`` row is on.

    Every other selection predicate (paid plan — grace-aware — the capability,
    consent, an enabled workflow, quota headroom, AI availability) is resolved
    per user inside the sweep. Re-implementing ``get_user_plan``'s grace-period
    rules in SQL here is exactly the kind of second source of truth that lets a
    lapsed subscription keep running work.
    """
    rows = (
        db.query(SettingsModel.user_id, SettingsModel.value)
        .filter(SettingsModel.category == "automation", SettingsModel.key == "auto_mode")
        .all()
    )
    return sorted({int(user_id) for user_id, value in rows if coerce_bool(value) is True})


def last_finished_run(db: Session, user_id: int, workflow: str) -> Optional[ScheduledRun]:
    """The newest auto run that closed a window for this workflow (the clock)."""
    return (
        db.query(ScheduledRun)
        .filter(ScheduledRun.user_id == user_id, ScheduledRun.workflow == workflow,
                ScheduledRun.state.in_(list(FINISHED_STATES)))
        .order_by(ScheduledRun.triggered_at.desc(), ScheduledRun.id.desc())
        .first()
    )


def newest_run(db: Session, user_id: int, workflow: str) -> Optional[ScheduledRun]:
    """The newest row for this workflow in *any* state (what the card shows)."""
    return (
        db.query(ScheduledRun)
        .filter(ScheduledRun.user_id == user_id, ScheduledRun.workflow == workflow)
        .order_by(ScheduledRun.triggered_at.desc(), ScheduledRun.id.desc())
        .first()
    )


def in_flight(db: Session, user_id: int, workflow: str) -> bool:
    """Is this workflow's auto work still executing (or its item still queued)?"""
    row = (
        db.query(ScheduledRun)
        .filter(ScheduledRun.user_id == user_id, ScheduledRun.workflow == workflow,
                ScheduledRun.state == STATE_QUEUED, ScheduledRun.queue_job_id.is_not(None))
        .order_by(ScheduledRun.id.desc())
        .first()
    )
    if row is None:
        return False
    status = db.query(PipelineJob.status).filter(PipelineJob.id == row.queue_job_id).scalar()
    # ``None`` = the queue row was deleted while the run was open: treat it as
    # busy rather than enqueueing a second pass at work nobody has seen.
    return status is None or status in OPEN_QUEUE_STATUSES


def record_run(db: Session, user_id: int, workflow: str, state: str, now: datetime, *,
                bucket: Optional[int] = None, queue_job_id: Optional[int] = None,
                job_id: Optional[int] = None, reason: str = "",
                meta: Optional[Dict[str, Any]] = None) -> ScheduledRun:
    """Write one scheduling decision. Every outcome — including the skips — lands here."""
    row = ScheduledRun(
        user_id=user_id,
        workflow=workflow,
        cycle_bucket=int(bucket or 0),
        triggered_at=now,
        queue_job_id=queue_job_id,
        job_id=job_id,
        state=state,
        reason=(reason or "")[:1000],
        meta=meta or {},
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log.info("auto mode %s → %s for user %s%s", workflow, state, user_id,
             f" ({row.reason})" if row.reason else "")
    return row


def record_skip(db: Session, user_id: int, workflow: str, state: str, now: datetime,
                cadence_seconds: int, *, reason: str = "", hint: str = "",
                meta: Optional[Dict[str, Any]] = None) -> Optional[ScheduledRun]:
    """Record a skip at most once per (user, workflow, window).

    A skip is a decision, not a spam clock: with a 5-minute sweep and a 2-hour
    cadence, an unbroken outage would otherwise write 24 rows per workflow per
    day and push the interesting history out of the last-five list.
    """
    bucket = cycle_bucket(now, cadence_seconds)
    existing = (
        db.query(ScheduledRun.id)
        .filter(ScheduledRun.user_id == user_id, ScheduledRun.workflow == workflow,
                ScheduledRun.cycle_bucket == bucket, ScheduledRun.state == state)
        .first()
    )
    if existing is not None:
        return None
    payload: Dict[str, Any] = dict(meta or {})
    if hint:
        payload["hint"] = hint
    row = record_run(db, user_id, workflow, state, now, bucket=bucket, reason=reason, meta=payload)
    inc("jobhunter_auto_skipped_total", workflow=workflow, reason=state)
    return row


# --------------------------------------------------------------------------- #
# The scheduler
# --------------------------------------------------------------------------- #
class AutoScheduler:
    """Periodic auto-mode sweep — one child task of the worker.

    :meth:`run` is the loop; :meth:`sweep` is a single pass, callable from tests
    (and from an operator's "run it now") without a loop. The worker's supervisor
    respawns this task if it ever dies with an exception; the loop itself never
    lets a failing sweep end it.
    """

    def __init__(self, interval_seconds: Optional[float] = None):
        self.interval = float(
            settings.auto_scheduler_interval_seconds if interval_seconds is None else interval_seconds
        )
        self.running = False
        # Subscription expiry is a daily maintenance pass, not a per-user auto
        # workflow.  Keeping the clock on the scheduler prevents a five-minute
        # sweep from writing the subscriptions table on every iteration while
        # still making a long-lived trial expire without an API request.
        self._last_subscription_expiry: Optional[datetime] = None

    @property
    def enabled(self) -> bool:
        """Whether this scheduler should exist at all (``0`` disables auto mode)."""
        return self.interval > 0

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        if not self.enabled:
            log.info("auto-mode scheduler disabled (AUTO_SCHEDULER_INTERVAL_SECONDS=0)")
            return
        self.running = True
        log.info("auto-mode scheduler started (sweep every %ss)", self.interval)
        while self.running:
            try:
                await self.sweep()
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any single sweep
                inc("jobhunter_auto_sweep_errors_total", stage="sweep")
                log.warning("auto-mode sweep failed and was skipped: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(max(1.0, self.interval))

    def stop(self) -> None:
        self.running = False

    # ------------------------------------------------------------------ #
    # One pass
    # ------------------------------------------------------------------ #
    async def sweep(self, *, now: Optional[datetime] = None) -> Dict[str, int]:
        """Sweep every auto-mode user once. Returns per-outcome counters."""
        moment = now or datetime.utcnow()
        counts = {"users": 0, "enqueued": 0, "skipped": 0, "errors": 0}
        db = SessionLocal()
        try:
            # This pass is deliberately independent of ``auto_mode``.  A user
            # who never enables scheduled workflows can still have a trial or
            # paid period end, so expiry belongs to the scheduler maintenance
            # path rather than to the candidate-user query below.
            if (
                self._last_subscription_expiry is None
                or moment - self._last_subscription_expiry >= timedelta(days=1)
            ):
                try:
                    expired = expire_subscriptions(db, now=moment)
                    self._last_subscription_expiry = moment
                    if expired:
                        log.info("subscription expiry pass flipped %s row(s)", expired)
                except Exception as exc:  # pragma: no cover - DB hiccup
                    db.rollback()
                    log.warning("subscription expiry pass failed: %s: %s", type(exc).__name__, exc)

            candidates = users_with_auto_mode(db)
        finally:
            db.close()

        for user_id in candidates:
            counts["users"] += 1
            # A session per user: one tenant's bad row can never poison the rest
            # of the sweep (a failed commit would otherwise strand the session).
            user_db = SessionLocal()
            try:
                await self.sweep_user(user_db, user_id, moment, counts)
            except Exception as exc:  # noqa: BLE001 - one bad user must not stop the sweep
                counts["errors"] += 1
                inc("jobhunter_auto_sweep_errors_total", stage="user")
                log.warning("auto-mode sweep failed for user %s: %s: %s", user_id, type(exc).__name__, exc)
                try:
                    user_db.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
            finally:
                user_db.close()

        if counts["users"]:
            log.info("auto-mode sweep: %s user(s), %s enqueued, %s skipped, %s error(s)",
                     counts["users"], counts["enqueued"], counts["skipped"], counts["errors"])
        return counts

    async def sweep_user(self, db: Session, user_id: int, now: datetime,
                         counts: Optional[Dict[str, int]] = None) -> Dict[str, int]:
        """One user's pass: fold the history in, then walk their plan's cadence."""
        tally = counts if counts is not None else {"users": 0, "enqueued": 0, "skipped": 0, "errors": 0}
        user = db.query(User).filter(User.id == user_id).first()
        if user is None or not user.is_active:
            return tally  # deleted or deactivated account: nothing to schedule, nothing to report

        cadence = plan_cadence(get_user_plan(db, user_id))
        if not cadence:
            return tally  # free (or lapsed past its grace period) — this plan schedules nothing
        if not capability_can(db, user_id, "can_use_scheduled_workflows"):
            return tally
        if not any(workflow_enabled(db, user_id, workflow) for workflow in cadence):
            return tally  # every workflow this plan schedules is switched off

        reconcile(db, user_id)

        if not consent_accepted(user):
            for workflow, seconds in cadence.items():
                if not workflow_enabled(db, user_id, workflow):
                    continue
                record_skip(db, user_id, workflow, STATE_SKIPPED_NO_CONSENT, now, int(seconds),
                            reason="the 'automation' disclosure has not been accepted",
                            hint="Accept it (Settings → Compliance) to let auto mode run.")
                tally["skipped"] += 1
            return tally

        for workflow, seconds in cadence.items():
            if not workflow_enabled(db, user_id, workflow):
                continue
            try:
                outcome = await self.attempt(db, user_id, workflow, int(seconds), now)
            except Exception as exc:  # noqa: BLE001 - per-workflow isolation
                tally["errors"] += 1
                inc("jobhunter_auto_sweep_errors_total", stage="workflow", workflow=workflow)
                log.warning("auto mode could not evaluate %s for user %s: %s: %s",
                            workflow, user_id, type(exc).__name__, exc)
                try:
                    db.rollback()
                    record_run(db, user_id, workflow, STATE_FAILED, now,
                               reason=f"{type(exc).__name__}: {exc}", meta={"stage": "evaluate"})
                except Exception:  # pragma: no cover - a broken session must not mask the error
                    log.exception("auto mode could not record the failure for user %s", user_id)
                continue
            if outcome == "enqueued":
                tally["enqueued"] += 1
            elif outcome.startswith("skipped"):
                tally["skipped"] += 1
        return tally

    # ------------------------------------------------------------------ #
    # The per-workflow decision
    # ------------------------------------------------------------------ #
    async def attempt(self, db: Session, user_id: int, workflow: str,
                      cadence_seconds: int, now: datetime) -> str:
        """Decide one (user, workflow) window. Returns an outcome label."""
        bucket = cycle_bucket(now, cadence_seconds)
        window_rows = (
            db.query(ScheduledRun)
            .filter(ScheduledRun.user_id == user_id, ScheduledRun.workflow == workflow,
                    ScheduledRun.cycle_bucket == bucket)
            .all()
        )
        if any(row.state not in SKIP_STATES for row in window_rows):
            # This window has already enqueued (or is still running its item).
            return "already_scheduled"

        if not is_due(last_finished_run(db, user_id, workflow), now, cadence_seconds):
            return "not_due"
        if in_flight(db, user_id, workflow):
            return "in_flight"

        if workflow == APPLICATION_PREP:
            job = ready_job(db, user_id)
            if job is None:
                # "Nothing is ready" is not a failure and not a run: record it as
                # a finished no-op so the card can say exactly that.
                record_run(db, user_id, workflow, STATE_DONE, now, bucket=bucket,
                           reason="nothing_ready", meta={"skipped": "nothing_ready"})
                inc("jobhunter_auto_skipped_total", workflow=workflow, reason="nothing_ready")
                return "nothing_ready"
            if job_has_open_item(db, user_id, int(job.id)):
                # The user queued this application themselves (or a previous auto
                # pass is still holding it) — never run the same job twice.
                return "in_flight"

        blocked = quota_block(db, user_id, workflow)
        if blocked is not None:
            limit_key, used, lim = blocked
            recorded = record_skip(
                db, user_id, workflow, STATE_SKIPPED_QUOTA, now, cadence_seconds,
                reason=f"{limit_key}: {used}/{lim} used this period",
                hint="Auto mode is paused until the monthly counters reset (or the plan changes).",
                meta={"limit": limit_key, "used": used, "limit_value": lim},
            )
            if recorded is not None:
                notify_quota_exhausted(db, user_id, limit_key, used, lim)
            return STATE_SKIPPED_QUOTA

        availability = await ai_availability(db, user_id)
        ai_state = str(availability.get("state") or "unknown")
        if ai_state != STATE_ONLINE:
            log.info("auto mode skipping %s for user %s — AI state is %s (%s)",
                     workflow, user_id, ai_state, availability.get("reason"))
            record_skip(db, user_id, workflow, STATE_SKIPPED_OUTAGE, now, cadence_seconds,
                        reason=f"ai_{ai_state}:{availability.get('reason') or 'unknown'}",
                        hint="Nothing was enqueued; the next sweep retries once the provider answers.",
                        meta={"ai_state": ai_state, "ai_reason": availability.get("reason"),
                              "retry_after_hint": availability.get("retry_after_hint")})
            return STATE_SKIPPED_OUTAGE

        payload, job_id = build_payload(db, user_id, workflow)
        item = enqueue(
            db,
            user_id=user_id,
            pipeline=PIPELINE_FOR_WORKFLOW[workflow],
            payload=payload,
            job_id=job_id,
            priority=AUTO_PRIORITY,
            dedupe_key=dedupe_key(workflow, user_id, bucket),
        )
        if item is None:
            # The queue already holds this window's item (another sweep or another
            # worker process got here first). The work exists — recording a second
            # run would double-count a month's quota for one pass.
            return "deduped"

        if workflow == APPLICATION_PREP and job_id is not None:
            mark_job_queued(db, user_id, int(job_id))

        record_run(db, user_id, workflow, STATE_QUEUED, now, bucket=bucket,
                   queue_job_id=int(item.id), job_id=job_id,
                   meta={"pipeline": item.pipeline, "priority": item.priority})
        inc("jobhunter_auto_enqueued_total", workflow=workflow)
        log.info("auto mode queued %s for user %s (queue item %s, window %s)",
                 workflow, user_id, item.id, bucket)
        return "enqueued"


# --------------------------------------------------------------------------- #
# Plumbing: history reconciliation, budgets, notifications, payloads
# --------------------------------------------------------------------------- #
def reconcile(db: Session, user_id: int) -> int:
    """Fold finished queue items back into the run history.

    A ``scheduled_runs`` row is written when work is *enqueued*; the outcome only
    exists once a worker has run it. Reading it back at the start of the user's
    next pass is what keeps the history honest (``queued`` →
    ``done|paused|failed|needs_input``), advances the cadence clock and releases
    the in-flight guard — with no new hook in the queue itself.
    """
    open_rows = (
        db.query(ScheduledRun)
        .filter(ScheduledRun.user_id == user_id, ScheduledRun.state == STATE_QUEUED,
                ScheduledRun.queue_job_id.is_not(None))
        .all()
    )
    changed = 0
    for row in open_rows:
        item = db.query(PipelineJob).filter(PipelineJob.id == row.queue_job_id).first()
        if item is None:
            row.state = STATE_FAILED
            row.reason = "queue item is gone (deleted or never created)"
            changed += 1
        elif item.status == "done":
            row.state = STATE_DONE
            changed += 1
        elif item.status == "paused":
            row.state = STATE_PAUSED
            row.reason = (item.error or "")[:1000]
            changed += 1
        elif item.status == "needs_input":
            row.state = STATE_NEEDS_INPUT
            row.reason = "waiting for your input (see the job's input request)"
            changed += 1
        elif item.status in ("dead", "failed"):
            row.state = STATE_FAILED
            row.reason = (item.error or "queue item dead-lettered")[:1000]
            changed += 1
        # queued / processing → still open: leave the row (the guard keeps holding).
    if changed:
        db.commit()
    return changed


def quota_block(db: Session, user_id: int, workflow: str) -> Optional[Tuple[str, int, int]]:
    """The first limit that forbids this run, or ``None`` when all of them allow it.

    ``automation_runs_per_month`` is the run budget the Dashboard shows; the
    extras are the per-action budgets a manual click is checked against before it
    enqueues. Checking both here is what keeps a schedule from being a way around
    a limit. The counters themselves are charged where a manual run charges them,
    never twice.
    """
    for limit_key in (AUTOMATION_LIMIT, *EXTRA_LIMITS_FOR_WORKFLOW.get(workflow, ())):
        allowed, used, lim, _hint = check_limit(db, user_id, limit_key)
        if not allowed:
            return limit_key, int(used), int(lim)
    return None


def notify_quota_exhausted(db: Session, user_id: int, limit_key: str, used: int, lim: int) -> bool:
    """Tell the user their automation budget ran out — once per exhaustion.

    The dedupe key is the *period* the counter belongs to: a user sitting at zero
    is not messaged every 300 seconds, and the month the counter resets is the
    first month a new notice can appear. In-app only — no email, no SMS.
    """
    period = current_period()
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.kind == QUOTA_NOTIFICATION_KIND,
                Notification.created_at >= month_start)
        .all()
    )
    for row in rows:
        meta = dict(row.meta or {})
        if meta.get("period") == period and meta.get("limit") == limit_key:
            return False
    from app.api.routers.notifications import create_notification

    create_notification(
        db, user_id, QUOTA_NOTIFICATION_KIND,
        title="Auto mode paused — monthly automation limit reached",
        body=(f"{limit_key.replace('_', ' ')}: {used} of {lim} used this month. Auto mode starts "
              f"again when the counter resets on the 1st, or now if you upgrade."),
        link="/billing",
        meta={"period": period, "limit": limit_key, "used": used, "limit_value": lim},
    )
    inc("jobhunter_auto_quota_notifications_total", limit=limit_key)
    return True


def ready_job(db: Session, user_id: int) -> Optional[Job]:
    """The next job an auto application-prep pass may prepare (highest score first)."""
    return (
        db.query(Job)
        .filter(Job.user_id == user_id, Job.status == "ready_to_apply")
        .order_by(Job.score.desc(), Job.id.asc())
        .limit(AUTO_APPLICATION_PER_PASS)
        .first()
    )


def job_has_open_item(db: Session, user_id: int, job_id: int) -> bool:
    """An in-flight application item for this job — the user's own or auto's.

    Without this guard an auto pass and a manual ``POST /jobs/{id}/apply`` could
    both hold work for one job (the queue dedupes by key, and the two paths use
    different keys), which is a double-submitted application.
    """
    if not job_id:
        return False
    return (
        db.query(PipelineJob.id)
        .filter(PipelineJob.user_id == user_id, PipelineJob.pipeline == "application",
                PipelineJob.job_id == job_id, PipelineJob.status.in_(list(OPEN_QUEUE_STATUSES)))
        .first()
        is not None
    )


def mark_job_queued(db: Session, user_id: int, job_id: int) -> None:
    """The same bookkeeping the manual queue path does for a job it enqueued.

    ``ready_to_apply`` is a state a dry-run autofill leaves a job in, so without
    this the next pass (or a click) would queue the identical job again. The
    handler moves it on to ``ready_to_apply`` / ``needs_input`` / ``applied`` when
    the item runs.
    """
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user_id).first()
    if job is None or job.status in ("applied", "skipped"):
        return
    job.status = "queued"
    db.commit()
    from app.services.events import record_job_event

    record_job_event(db, user_id=user_id, job_id=job_id, stage="queued", status="info",
                     message="Queued by auto mode (application preparation)")


def discovery_payload(db: Session, user_id: int) -> Dict[str, Any]:
    """The keys :func:`app.services.handlers.handle_discovery` reads.

    Keywords come from what the user already has on file: their own extra
    keywords, plus the stored search context of the active persona (the AI
    extraction written when their resume was parsed). Auto mode does **not** call
    the model to decide what to enqueue — the sweep must stay cheap, and a
    scheduler that needs AI to work out its own input turns an outage into a
    stall of the whole loop.
    """
    from app.services.discovery import discovery_sources_config

    config = discovery_sources_config(db, user_id)
    from app.services import persona as persona_service

    persona = persona_service.get_persona(db, user_id, None)
    stored = dict(persona.search_context or {}) if persona else {}
    keywords: List[str] = []
    for token in _as_string_list(get_setting(db, user_id, "scraping", "keywords",
                                             settings.default_keywords)):
        if token not in keywords:
            keywords.append(token)
    for token in _as_string_list(stored.get("keywords"))[:16]:
        if token not in keywords:
            keywords.append(token)
    # An unreadable freshness value raises *here* — inside the per-workflow
    # try/except — instead of poisoning the whole sweep: the honest outcome is one
    # failed auto run for this workflow with the reason recorded.
    freshness = int(get_setting(db, user_id, "scraping", "freshness_hours",
                                settings.default_freshness_hours))
    return {
        "keywords": keywords[:20],
        "freshness_hours": freshness,
        "limit": AUTO_DISCOVERY_LIMIT,
        "live_enabled": bool(config["live_enabled"]),
        "sources": list(config["sources"]),
        "board_tokens": list(config["board_tokens"]),
        "persona_id": int(persona.id) if persona else None,
        "trigger": "auto",
    }


def funding_payload(db: Session, user_id: int) -> Dict[str, Any]:
    """The shape ``POST /api/funding/refresh`` enqueues for ``handle_funding``.

    ``context`` is intentionally absent: the handler builds it with
    ``search_context(force_refresh=True)`` when the payload has none, and that is
    where an AI extraction belongs — inside the pausable queue item, not in the
    sweep that decides whether to create it.
    """
    window = int(get_setting(db, user_id, "funding", "freshness_days", settings.funding_freshness_days))
    provider = get_setting(db, user_id, "funding", "provider", settings.funding_provider)
    parts = [str(get_setting(db, user_id, "funding", key, "") or "").strip()
             for key in ("context_notes", "industries")]
    from app.services import persona as persona_service

    persona = persona_service.get_persona(db, user_id, None)
    return {
        "window_days": max(1, window),
        "provider": str(provider or settings.funding_provider),
        "stages": None,
        "user_context": ", ".join(part for part in parts if part),
        "persona_id": int(persona.id) if persona else None,
        "trigger": "auto",
    }


def build_payload(db: Session, user_id: int, workflow: str) -> Tuple[Dict[str, Any], Optional[int]]:
    """(queue payload, job id) for one auto workflow. ``trigger`` is always auto."""
    if workflow == DISCOVERY:
        return discovery_payload(db, user_id), None
    if workflow == FUNDING:
        return funding_payload(db, user_id), None
    job = ready_job(db, user_id)
    if job is None:  # guarded by attempt(); defensive so a race never enqueues empty work
        return {"trigger": "auto", "skipped": "nothing_ready"}, None
    answers = dict(dict(job.extra or {}).get("answers") or {})
    return ({"resume_choice": "auto", "answers": answers, "trigger": "auto"}, int(job.id))


# --------------------------------------------------------------------------- #
# The read side (GET /api/automation, the Settings card, the dashboard block)
# --------------------------------------------------------------------------- #
def schedule_for(db: Session, user_id: int) -> Dict[str, Any]:
    """The user's auto-mode state, minus the history.

    Every predicate here is the one the *writer* uses: ``can_use`` is the
    capability the settings gate checks, ``cadence`` is the table the sweep reads,
    ``consent_ok`` is what ``require_consent`` reads. A UI built on this block
    cannot disagree with the scheduler.
    """
    plan = get_user_plan(db, user_id)
    cadence = plan_cadence(plan)
    can_use = bool(capability_can(db, user_id, "can_use_scheduled_workflows"))
    enabled = auto_mode_enabled(db, user_id)
    consent_ok = consent_accepted(db.query(User).filter(User.id == user_id).first())
    running = scheduler_enabled()
    schedulable = bool(cadence and can_use)

    blocking: List[str] = []
    if not running:
        blocking.append("scheduler_disabled")
    if not can_use:
        blocking.append("plan_locked")
    if not cadence:
        blocking.append("plan_has_no_schedule")
    if not enabled:
        blocking.append("auto_mode_off")
    if not consent_ok:
        blocking.append("consent_required")

    return {
        "enabled": enabled,
        "can_use": can_use,
        "active": bool(enabled and can_use and consent_ok and cadence and running),
        "plan": plan,
        "cadence": cadence,
        "consent_ok": consent_ok,
        "sweep_interval_seconds": float(settings.auto_scheduler_interval_seconds),
        "toggles": {workflow: workflow_enabled(db, user_id, workflow) for workflow in cadence},
        "schedulable": schedulable,
        "blocking": blocking,
    }


def overview(db: Session, user_id: int, *, recent_limit: int = 5,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """The ``GET /api/automation`` payload: state, cadence, clock, history, quota."""
    moment = now or datetime.utcnow()
    schedule = schedule_for(db, user_id)
    cadence: Dict[str, int] = dict(schedule["cadence"])

    last_run: Dict[str, Any] = {}
    next_run: Dict[str, Optional[str]] = {}
    for workflow, seconds in cadence.items():
        newest = newest_run(db, user_id, workflow)
        last_run[workflow] = {
            "at": iso_utc(newest.triggered_at) if newest else None,
            "state": newest.state if newest else None,
            "job_id": int(newest.job_id) if newest and newest.job_id is not None else None,
            "queue_id": int(newest.queue_job_id) if newest and newest.queue_job_id is not None else None,
            "reason": (newest.reason or "") if newest else "",
        }
        if not schedule["active"] or in_flight(db, user_id, workflow):
            # ``null`` = "no next run to promise": auto mode is off/blocked, or
            # this workflow's run is executing right now.
            next_run[workflow] = None
        else:
            next_run[workflow] = iso_utc(next_run_at(last_finished_run(db, user_id, workflow),
                                                     moment, int(seconds)))

    recent = [
        {
            "workflow": row.workflow,
            "at": iso_utc(row.triggered_at),
            "state": row.state,
            "job_id": int(row.job_id) if row.job_id is not None else None,
            "queue_id": int(row.queue_job_id) if row.queue_job_id is not None else None,
            "reason": row.reason or "",
        }
        for row in (
            db.query(ScheduledRun)
            .filter(ScheduledRun.user_id == user_id)
            .order_by(ScheduledRun.triggered_at.desc(), ScheduledRun.id.desc())
            .limit(max(1, recent_limit))
            .all()
        )
    ]

    used, lim = usage_for(db, user_id, AUTOMATION_LIMIT)
    quota = {
        "used": int(used),
        "limit": int(lim),
        # Same convention as ``entitlements_snapshot`` so the Settings card and
        # the Dashboard can never show different headroom for one plan.
        "remaining": max(0, int(lim) - int(used)) if lim else 999999,
    }
    return {**schedule, "last_run": last_run, "next_run": next_run, "recent": recent, "quota": quota}


def brief(db: Session, user_id: int) -> Dict[str, Any]:
    """What the dashboard's automation block needs — no history list.

    ``next_run`` is the earliest moment *any* of the user's scheduled workflows
    is due again (``null`` when auto mode is not running), so the card can say
    "next run in 1h 40m" without polling the automation endpoint.
    """
    moment = datetime.utcnow()
    schedule = schedule_for(db, user_id)
    next_at: Optional[datetime] = None
    if schedule["active"]:
        for workflow, seconds in schedule["cadence"].items():
            if in_flight(db, user_id, workflow):
                continue
            candidate = next_run_at(last_finished_run(db, user_id, workflow), moment, int(seconds))
            if next_at is None or candidate < next_at:
                next_at = candidate
    return {
        "auto_mode": bool(schedule["enabled"]),
        "active": bool(schedule["active"]),
        "next_run": iso_utc(next_at) if next_at is not None else None,
        "blocking": list(schedule["blocking"]),
    }


__all__ = (
    "ALL_STATES",
    "APPLICATION_PREP",
    "AutoScheduler",
    "CADENCE_SECONDS",
    "DISCOVERY",
    "FINISHED_STATES",
    "FUNDING",
    "FUNDING_MATCH_NOTIFICATION_KIND",
    "HIGH_MATCH_NOTIFICATION_KIND",
    "QUOTA_NOTIFICATION_KIND",
    "SKIP_STATES",
    "auto_mode_enabled",
    "brief",
    "consent_accepted",
    "cycle_bucket",
    "dedupe_key",
    "in_flight",
    "is_due",
    "iso_utc",
    "last_finished_run",
    "newest_run",
    "next_run_at",
    "overview",
    "plan_cadence",
    "record_run",
    "record_skip",
    "reconcile",
    "run_clock",
    "scheduler_enabled",
    "users_with_auto_mode",
    "workflow_enabled",
)
