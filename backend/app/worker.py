"""
Background worker + standalone entrypoint.

Run inside the API process (``RUN_WORKER_IN_API=true``, the default for
single-container deploys) or as its own process for real horizontal scaling:

    python -m app.worker --pipelines discovery,application,email,funding,ai

The worker claims durable items from ``pipeline_jobs`` (see ``job_queue``), so
restarts and rolling deploys never lose work.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
from typing import List, Optional, Sequence

from app.core.config import settings
from app.core.logging import LogContext, configure_logging, get_logger
from app.core.metrics import inc, set_gauge
from app.db import SessionLocal, init_db
from app.metrics_server import start_metrics_server
from app.services.ai_client import is_ai_error, is_transient_ai_error
from app.services.ai_pipeline import ai_pipeline
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
    """Claims and executes pipeline jobs with bounded concurrency."""

    def __init__(self, pipelines: Sequence[str] = PIPELINES, concurrency: Optional[int] = None,
                 poll_interval: Optional[float] = None):
        self.pipelines = [p for p in pipelines if p in HANDLERS]
        self.concurrency = max(1, concurrency or settings.worker_concurrency)
        self.poll_interval = poll_interval or settings.worker_poll_interval
        self.running = False
        self.worker_id = worker_id()
        self._tasks: List[asyncio.Task] = []

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
            await self._run_item(item.id, item.pipeline)

    async def start(self) -> None:
        configure_logging()
        init_db()
        self.running = True
        await self._recover()
        set_gauge("jobhunter_worker_running", 1, worker=self.worker_id)
        log.info("worker %s started: pipelines=%s concurrency=%s",
                 self.worker_id, ",".join(self.pipelines), self.concurrency)
        self._tasks = [asyncio.create_task(self._loop()) for _ in range(self.concurrency)]
        ai_task = asyncio.create_task(ai_pipeline.worker_loop())
        self._tasks.append(ai_task)
        # Re-probes the AI-availability signal and drains paused work when the
        # provider is back — the resume half of the pause/resume contract.
        self.watchdog = AIWatchdog()
        self._tasks.append(asyncio.create_task(self.watchdog.run()))
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self.running = False
        ai_pipeline.running = False
        if hasattr(self, "watchdog"):
            self.watchdog.stop()
        for task in self._tasks:
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
