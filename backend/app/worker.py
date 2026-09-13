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
"""
from __future__ import annotations

import argparse
import asyncio
import signal
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import settings
from app.core.logging import LogContext, configure_logging, get_logger
from app.core.metrics import inc, set_gauge
from app.db import SessionLocal, init_db
from app.metrics_server import start_metrics_server
from app.services.ai_client import is_ai_error, is_transient_ai_error
from app.services.ai_watchdog import AIWatchdog
from app.services.handlers import HANDLERS
from app.services.job_queue import (
    PIPELINES,
    claim,
    claim_item,
    complete,
    fail,
    needs_input,
    pause,
    recover_stalled,
    worker_id,
)

log = get_logger("app.worker")


class Worker:
    """Claims and executes pipeline jobs with bounded concurrency.

    :meth:`start` supervises every child task (the claim loops plus the AI
    watchdog): a task that dies with an exception is logged once with its
    traceback, counted and respawned, so a crashed slot is visible and
    self-heals instead of vanishing.
    """

    def __init__(self, pipelines: Sequence[str] = PIPELINES, concurrency: Optional[int] = None,
                 poll_interval: Optional[float] = None):
        self.pipelines = [p for p in pipelines if p in HANDLERS]
        self.concurrency = max(1, concurrency or settings.worker_concurrency)
        self.poll_interval = poll_interval or settings.worker_poll_interval
        self.running = False
        self.worker_id = worker_id()
        #: slot name (``loop-0..N-1``, ``watchdog``) -> live task
        self._tasks: Dict[str, asyncio.Task] = {}
        #: consecutive crash count per slot (drives the capped backoff)
        self._crash_counts: Dict[str, int] = defaultdict(int)
        self._last_crash: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------ #
    # Slot management & supervision
    # ------------------------------------------------------------------ #
    def _slot_names(self) -> List[str]:
        return [f"loop-{i}" for i in range(self.concurrency)] + ["watchdog"]

    def _spawn(self, name: str) -> None:
        if not self.running:
            return
        if name == "watchdog":
            task = asyncio.create_task(self.watchdog.run())
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
    async def _recover(self) -> None:
        db = SessionLocal()
        try:
            recover_stalled(db, pipelines=self.pipelines)
        finally:
            db.close()

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
                    if is_ai_error(exc):
                        # AI is a hard dependency — but the *kind* of failure
                        # decides the outcome. A transient outage (timeout /
                        # unreachable / 429 / 5xx / breaker / budget) pauses
                        # the item with backoff: it is re-queued (not failed,
                        # not completed) and the watchdog resumes it when the
                        # availability signal is green. A needs-action failure
                        # (no/invalid key, quota, …) is dead-lettered at once
                        # — retrying a blocked account is pointless.
                        if is_transient_ai_error(exc):
                            outcome = pause(db, item, str(exc))
                        else:
                            outcome = fail(db, item, str(exc), retryable=False)
                        log.warning("item %s -> %s (ai)", item.id, outcome)
                        return
                    outcome = fail(db, item, f"{type(exc).__name__}: {exc}")
                    log.warning("item %s -> %s", item.id, outcome)
                    return
            if item.status == "processing":
                if isinstance(result, dict) and result.get("status") == "needs_input":
                    needs_input(db, item, reason="waiting for user input")
                else:
                    complete(db, item, result=result if isinstance(result, dict) else {"result": str(result)})
        finally:
            db.close()

    async def _loop(self) -> None:
        while self.running:
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
