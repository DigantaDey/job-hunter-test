"""
Background worker + standalone entrypoint.

Run inside the API process (``RUN_WORKER_IN_API=true``, the default for
single-container deploys) or as its own process for real horizontal scaling:

    python -m app.worker --pipelines discovery,application,email,funding,ai

The worker claims durable items from ``pipeline_jobs`` (see ``job_queue``), so
restarts and rolling deploys never lose work.

Self-healing (v2.1.1): two layers keep capacity honest.

1. A single bad item cannot kill a slot: ``_loop`` wraps ``_run_item`` so any
   exception is logged with the item id, counted
   (``jobhunter_worker_item_errors_total``) and the loop keeps serving — the
   row is left to the lease machinery, which re-queues it once the lease
   expires.
2. A slot that dies anyway (DB connect/teardown failures, anything that
   escapes layer 1) is caught by the supervisor in :meth:`Worker.start`: it
   is logged once with its traceback, counted
   (``jobhunter_worker_task_crashes_total{task=...}``) and respawned with a
   small, capped backoff. One bad item can no longer shrink the queue's
   capacity to zero — silently.

Since v2.2 the supervisor also owns the **auto-mode scheduler** child task (the
per-user cadence sweep, see :mod:`app.services.auto_scheduler`), so scheduled
work inherits the same crash logging and respawn instead of growing a second,
weaker loop next to it. The scheduler enqueues into this same queue; the worker
process that claims the items is unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import settings
from app.core.entitlements import expire_subscriptions
from app.core.logging import LogContext, configure_logging, get_logger
from app.core.metrics import inc, set_gauge
from app.db import SessionLocal, init_db
from app.metrics_server import start_metrics_server
from app.services.ai_watchdog import AIWatchdog
from app.services.auto_scheduler import AutoScheduler
from app.services.handlers import HANDLERS
from app.services.job_queue import (
    PIPELINES,
    claim,
    claim_item,
    complete,
    fail,
    needs_input,
    recover_stalled,
    worker_id,
)
from app.services.reliability import apply_failure, expire_user_actions

log = get_logger("app.worker")


class Worker:
    """Claims and executes pipeline jobs with bounded concurrency.

    :meth:`start` supervises every child task (the claim loops, the AI watchdog
    and the auto-mode scheduler): a task that dies with an exception is logged
    once with its traceback, counted and respawned, so a crashed slot is visible
    and self-heals instead of vanishing.
    """

    def __init__(self, pipelines: Sequence[str] = PIPELINES, concurrency: Optional[int] = None,
                 poll_interval: Optional[float] = None,
                 reaper_interval: Optional[float] = None,
                 reaper_safety_seconds: Optional[float] = None):
        self.pipelines = [p for p in pipelines if p in HANDLERS]
        self.concurrency = max(1, concurrency or settings.worker_concurrency)
        self.poll_interval = poll_interval or settings.worker_poll_interval
        self.reaper_interval = (
            reaper_interval
            if reaper_interval is not None
            else float(getattr(settings, "worker_reaper_interval_seconds", 60.0))
        )
        self.reaper_safety = (
            reaper_safety_seconds
            if reaper_safety_seconds is not None
            else float(getattr(settings, "worker_reaper_safety_seconds", 300.0))
        )
        self._last_reap = time.monotonic()  # start interval from now
        # User-action expiry (contracts/11 §2): a run parked for the user is
        # never retried, and it must not wait forever either. The sweep runs on
        # its own interval — a 7-day default TTL does not need a 60s clock.
        self.user_action_interval = float(
            getattr(settings, "user_action_sweep_interval_seconds", 900.0))
        self._last_user_action_sweep = 0.0  # run once at boot, then on interval
        # Subscription expiry is maintenance work, not a user-triggered queue
        # item.  Run it at boot and at most once per day from the worker so it
        # still happens when auto mode is disabled or no user has enabled it.
        self._last_subscription_expiry = 0.0
        self.running = False
        self.worker_id = worker_id()
        #: slot name (``loop-0..N-1``, ``watchdog``, ``auto-scheduler``) -> live task
        self._tasks: Dict[str, asyncio.Task] = {}
        #: consecutive crash count per slot (drives the capped backoff)
        self._crash_counts: Dict[str, int] = defaultdict(int)
        self._last_crash: Optional[Dict[str, str]] = None
        #: Auto mode (v2.2) — one more supervised child task, built here so the
        #: slot list is answerable before ``start()``. It holds no session: a
        #: scheduler that is constructed but never started costs nothing.
        self.scheduler = AutoScheduler()

    # ------------------------------------------------------------------ #
    # Slot management & supervision
    # ------------------------------------------------------------------ #
    def _slot_names(self) -> List[str]:
        names = [f"loop-{i}" for i in range(self.concurrency)] + ["watchdog"]
        # Auto mode (v2.2) is one more supervised child rather than a second
        # supervisor: it inherits the crash logging and the capped-backoff
        # respawn for free. With AUTO_SCHEDULER_INTERVAL_SECONDS=0 the slot is
        # not in this list at all, so nothing is spawned and the respawn loop
        # below never looks for it — auto mode is off, not spinning.
        if self.scheduler.enabled:
            names.append("auto-scheduler")
        return names

    def _spawn(self, name: str) -> None:
        if not self.running:
            return
        if name == "watchdog":
            task = asyncio.create_task(self.watchdog.run())
        elif name == "auto-scheduler":
            task = asyncio.create_task(self.scheduler.run())
        else:
            task = asyncio.create_task(self._loop())
        task.set_name(f"jobhunter-{self.worker_id}-{name}")
        self._tasks[name] = task

    def _spawn_all(self) -> None:
        for name in self._slot_names():
            self._spawn(name)

    def _slot_for(self, task: asyncio.Task) -> Optional[str]:
        for name, slot_task in self._tasks.items():
            if slot_task is task:
                return name
        return None

    def _respawn_delay(self, name: Optional[str]) -> float:
        """Small, capped backoff: doubles per consecutive crash of the slot."""
        base = max(0.0, settings.worker_respawn_backoff_seconds)
        cap = max(base, max(0.0, settings.worker_crash_backoff_max_seconds))
        streak = self._crash_counts.get(name, 0) if name else 0
        return min(cap, base * max(1, streak))

    def _handle_dead_task(self, task: asyncio.Task) -> None:
        """React to one finished child task.

        Cancellation (``stop()``) and clean shutdown are never crashes.
        Anything else is a crash: logged exactly once — with the traceback —
        and counted, so no child exception is ever swallowed.
        """
        name = self._slot_for(task) or "unknown"
        if name != "unknown":
            self._tasks.pop(name, None)
        if task.cancelled() or not self.running:
            return  # stop() in flight — a clean shutdown, never a respawn
        exc = task.exception()
        if exc is None:
            # Loops exit only when ``running`` goes False; an unexpected clean
            # exit still loses capacity, so restore the slot quietly.
            log.warning("worker task '%s' exited while the worker is running — respawning", name)
            return
        log.error("worker task '%s' crashed: %s: %s", name, type(exc).__name__, exc, exc_info=exc)
        inc("jobhunter_worker_task_crashes_total", task=name)
        self._crash_counts[name] += 1
        self._last_crash = {
            "task": name,
            "at": datetime.utcnow().isoformat() + "Z",
            "error": f"{type(exc).__name__}: {exc}",
        }

    def task_health(self) -> Dict[str, Any]:
        """Live slot snapshot for ``GET /api/ops/status`` (``workers.tasks``).

        ``alive`` counts claim loops that are actually running — a slot in
        respawn backoff shows up as ``expected > alive``.
        """
        loops = [t for name, t in self._tasks.items() if name.startswith("loop-")]
        return {
            "expected": self.concurrency,
            "alive": sum(1 for t in loops if not t.done()),
            "last_crash": self._last_crash,
        }

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def _run_subscription_expiry_pass(self, *, force: bool = False) -> None:
        """Flip elapsed trials/periods without waiting for an API request.

        The auto scheduler also calls the same batch function, but the worker
        owns this boot/daily fallback so deployments with
        ``AUTO_SCHEDULER_INTERVAL_SECONDS=0`` do not leave subscriptions live
        forever.  Failures are isolated from queue recovery; the next pass can
        retry after a transient database problem.
        """
        now = time.monotonic()
        if not force and now - self._last_subscription_expiry < 24 * 60 * 60:
            return
        db = None
        try:
            db = SessionLocal()
            expired = expire_subscriptions(db, now=datetime.utcnow())
            self._last_subscription_expiry = now
            if expired:
                log.info("subscription expiry pass flipped %s row(s)", expired)
        except Exception as exc:  # pragma: no cover - DB hiccup
            if db is not None:
                db.rollback()
            log.warning("subscription expiry pass failed: %s: %s", type(exc).__name__, exc)
        finally:
            if db is not None:
                db.close()

    async def _recover(self) -> None:
        db = SessionLocal()
        try:
            # Startup recovery uses the same safety margin as the periodic
            # reaper to avoid cloning a live worker on rolling deploy.
            recover_stalled(
                db,
                pipelines=self.pipelines,
                safety_margin_seconds=self.reaper_safety,
            )
            # Reuse the recovery session for the boot expiry pass.  Apart from
            # avoiding an unnecessary connection, this keeps startup's normal
            # session shape stable for workers and health checks.
            try:
                expired = expire_subscriptions(db, now=datetime.utcnow())
                self._last_subscription_expiry = time.monotonic()
                if expired:
                    log.info("subscription expiry pass flipped %s row(s)", expired)
            except Exception as exc:  # pragma: no cover - DB hiccup
                db.rollback()
                log.warning("subscription expiry pass failed: %s: %s", type(exc).__name__, exc)
            # A worker that restarts must not resurrect state nobody is waiting
            # on any more: close out the user-action-required rows whose window
            # passed while the process was down (contracts/11 §2). Same session
            # as the passes above, for the same reason — startup keeps one
            # connection and one session shape.
            try:
                expire_user_actions(db)
                self._last_user_action_sweep = time.monotonic()
            except Exception as exc:  # pragma: no cover - DB hiccup
                db.rollback()
                log.warning("user-action expiry pass failed: %s: %s", type(exc).__name__, exc)
        finally:
            db.close()
        # Start the periodic interval from now, after the boot sweep.
        self._last_reap = time.monotonic()

    def _should_expire_user_actions(self) -> bool:
        """Shared timer across loop slots, like :meth:`_should_reap`."""
        now = time.monotonic()
        if now - self._last_user_action_sweep >= self.user_action_interval:
            self._last_user_action_sweep = now
            return True
        return False

    async def _expire_user_actions(self) -> None:
        """Close user-action-required states whose waiting window has passed."""
        from app.services.reliability import expire_user_actions

        db = SessionLocal()
        try:
            expire_user_actions(db)
        except Exception as exc:  # pragma: no cover - DB hiccup
            db.rollback()
            log.warning("user-action expiry sweep failed: %s: %s", type(exc).__name__, exc)
        finally:
            db.close()
        self._last_user_action_sweep = time.monotonic()

    def _should_reap(self) -> bool:
        # Shared across loop slots: first slot to hit the interval reaps.
        now = time.monotonic()
        if now - self._last_reap >= self.reaper_interval:
            self._last_reap = now
            return True
        return False

    async def _do_reap(self) -> None:
        db = SessionLocal()
        try:
            recover_stalled(
                db,
                pipelines=self.pipelines,
                safety_margin_seconds=self.reaper_safety,
            )
        except Exception as exc:  # pragma: no cover - DB hiccup
            log.error("reaper failed: %s", exc)
        finally:
            db.close()
        self._run_subscription_expiry_pass()

    async def _run_item(self, item_id: int, pipeline: str) -> None:
        db = SessionLocal()
        try:
            from app.models.models import PipelineJob

            item = db.query(PipelineJob).filter(PipelineJob.id == item_id).first()
            if not item:
                return
            if item.status == "queued":
                # Not yet claimed (direct invocation / manual re-run).
                item = claim_item(db, item_id)
                if item is None:
                    return
            handler = HANDLERS.get(item.pipeline)
            if not handler:
                fail(db, item, f"no handler for pipeline '{item.pipeline}'", retryable=False)
                return
            with LogContext(request_id=f"job-{item.id}", user_id=item.user_id):
                log.info("processing %s item %s", item.pipeline, item.id)
                try:
                    result = await handler(db, item)
                except Exception as exc:  # noqa: BLE001 - pipeline failures must not kill the worker
                    # Every exception goes through one classifier
                    # (``services/reliability``), which decides between the
                    # four outcomes and labels the failure with a bounded
                    # reason:
                    #
                    # * a transient AI outage (timeout / unreachable / 429 /
                    #   5xx / breaker / budget) *pauses* the item — re-queued
                    #   with backoff, no attempt spent, resumed by the watchdog
                    #   when the availability signal is green;
                    # * anything retryable (a reset connection, a pool
                    #   exhaustion, an upstream 5xx) is re-queued with backoff
                    #   against the failure budget;
                    # * anything permanent (a deleted row, a rejected key, a
                    #   ``TypeError``) is dead-lettered at once so the user
                    #   sees it instead of waiting through three retries;
                    # * anything the human must answer parks the item in
                    #   ``needs_input`` — never retried, and it expires.
                    apply_failure(db, item, exc)
                    return
            if item.status == "processing":
                if isinstance(result, dict) and result.get("status") == "needs_input":
                    needs_input(db, item, reason="waiting for user input", result=result)
                else:
                    # v2.2 auto mode, the quota half: an auto-triggered run
                    # charges the monthly automation budget once, on success.
                    # Items that paused or failed charge nothing (an outage is not
                    # the user's fault), and manual runs are charged by their own
                    # router at trigger time — so this is the only place auto runs
                    # are counted, and nothing is ever counted twice. Before this
                    # existed the Dashboard's used/limit readout was wired to a
                    # counter nothing incremented.
                    if dict(item.payload or {}).get("trigger") == "auto":
                        from app.core.entitlements import increment_usage

                        try:
                            increment_usage(db, int(item.user_id), "automation_runs_per_month", 1)
                        except Exception as exc:  # pragma: no cover - never lose the run over the ledger
                            log.warning("could not charge automation quota for item %s: %s",
                                        item.id, exc)
                    complete(db, item, result=result if isinstance(result, dict) else {"result": str(result)})
        finally:
            db.close()

    async def _loop(self) -> None:
        while self.running:
            # Periodic lease reaper (v2.3): recover stalled rows without a
            # process restart. Each loop slot checks the shared timer; the
            # first slot to hit the interval does the reap (CAS-safe).
            if self._should_reap():
                await self._do_reap()
            if self._should_expire_user_actions():
                await self._expire_user_actions()

            db = SessionLocal()
            try:
                item = claim(db, pipelines=self.pipelines)
            except Exception as exc:  # pragma: no cover - DB hiccup
                log.error("claim failed: %s", exc)
                item = None
            finally:
                db.close()

            if item is None:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await self._run_item(item.id, item.pipeline)
            except Exception:
                # Layer 1 of the self-healing contract: a single item must
                # never kill a worker slot. Log it (with traceback) and count
                # it; the loop keeps serving. The row is left to the lease
                # machinery — recover_stalled re-queues it once the lease
                # expires (the handler's own error path may already have
                # failed/paused/re-queued it).
                inc("jobhunter_worker_item_errors_total", pipeline=item.pipeline)
                log.exception("worker item %s (%s) escaped _run_item — slot continues",
                              item.id, item.pipeline)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Run the worker until :meth:`stop`, supervising every child task.

        The pre-v2.1.1 implementation gathered its tasks with
        ``return_exceptions=True``: the first crash quietly ended that slot
        and was never logged or surfaced. Now ``start`` itself is the
        supervisor — it wakes on every finished child task, and each crash is
        logged once with its traceback, counted in
        ``jobhunter_worker_task_crashes_total`` and respawned with a small,
        capped backoff. ``stop()`` suppresses respawns.
        """
        configure_logging()
        init_db()
        self.running = True
        await self._recover()
        set_gauge("jobhunter_worker_running", 1, worker=self.worker_id)
        log.info("worker %s started: pipelines=%s concurrency=%s",
                 self.worker_id, ",".join(self.pipelines), self.concurrency)
        self.watchdog = AIWatchdog()
        # Auto mode sweeps every AUTO_SCHEDULER_INTERVAL_SECONDS and enqueues
        # whatever is due (see app/services/auto_scheduler.py); a crash in it is
        # logged, counted and respawned exactly like a claim loop.
        self.scheduler.running = self.running
        self._spawn_all()
        while self.running:
            if not self._tasks:
                # Every slot is dead and stop() has not been called — rebuild
                # with backoff instead of running with zero capacity.
                await asyncio.sleep(self._respawn_delay(None))
                if self.running:
                    self._spawn_all()
                continue
            done, _pending = await asyncio.wait(list(self._tasks.values()),
                                                return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                self._handle_dead_task(task)
            if not self.running:
                break
            for name in self._slot_names():
                if name not in self._tasks:
                    await asyncio.sleep(self._respawn_delay(name))
                    if self.running and name not in self._tasks:
                        self._spawn(name)

    async def stop(self) -> None:
        self.running = False
        if hasattr(self, "watchdog"):
            self.watchdog.stop()
        if hasattr(self, "scheduler"):
            self.scheduler.stop()  # end the sweep loop before its sleep is cancelled
        for task in list(self._tasks.values()):
            task.cancel()
        inc("jobhunter_worker_stops_total", worker=self.worker_id)
        set_gauge("jobhunter_worker_running", 0, worker=self.worker_id)
        log.info("worker %s stopped", self.worker_id)


async def run_forever(pipelines: Sequence[str] = PIPELINES, concurrency: Optional[int] = None) -> None:
    worker = Worker(pipelines=pipelines, concurrency=concurrency)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal(*_args):
        log.info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    metrics_listener = start_metrics_server()

    task = asyncio.create_task(worker.start())
    await stop_event.wait()
    await worker.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if metrics_listener is not None:
        metrics_listener.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="JobHunter pipeline worker")
    parser.add_argument("--pipelines", default=",".join(PIPELINES),
                        help="comma-separated pipelines to process")
    parser.add_argument("--concurrency", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run_forever([p.strip() for p in args.pipelines.split(",") if p.strip()], args.concurrency))


if __name__ == "__main__":  # pragma: no cover
    main()
