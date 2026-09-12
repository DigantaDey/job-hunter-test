"""AI-availability watchdog — re-probe on a schedule, drain paused work.

The pause/resume contract (v2.1) has two halves:

* **Pause.** Synchronous requests that hit a ``transient_outage`` mid-flight
  get a dedicated pausable 503 (``status='ai_paused'`` + ``retry_after_hint``),
  and queued work that hits one is re-queued with backoff as ``paused`` —
  never marked failed or completed.
* **Resume.** This watchdog re-probes the central availability signal on a
  schedule and, the moment a user's AI is green again, drains that user's
  paused queue items so the worker completes them without any user action.
  The one-click resume endpoint (``POST /api/settings/ai/resume``) drains the
  same way, immediately, for users who do not want to wait for a cycle.

The watchdog is per-user: AI config (and therefore availability) is per-user,
so each user's paused work is probed with *that user's* resolved config.
Probes are cached for 60s in ``ai_client.ping`` — polling is cheap.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.db import SessionLocal
from app.models.models import PipelineJob
from app.services.ai_client import STATE_ONLINE, ai_availability
from app.services.job_queue import PIPELINES, drain_paused, paused_count

log = get_logger("app.ai_watchdog")


async def check_and_drain(db, user_id: int, *, probe_timeout: int = 6) -> tuple[Optional[str], int]:
    """One watchdog cycle for one user: probe their AI, drain paused work if green.

    Returns ``(state, drained)`` — ``state`` is ``None`` when the user had no
    paused work (nothing to probe).
    """
    if paused_count(db, user_id=user_id) == 0:
        return None, 0
    availability = await ai_availability(db, user_id, probe_timeout=probe_timeout)
    state = availability.get("state")
    drained = 0
    if state == STATE_ONLINE:
        drained = drain_paused(db, user_id=user_id, pipelines=list(PIPELINES))
        if drained:
            log.info("watchdog: user %s AI back online — resumed %s paused item(s)", user_id, drained)
    return state, drained


def users_with_paused_work(db) -> list[int]:
    """Distinct user ids that currently have paused queue items."""
    rows = (
        db.query(PipelineJob.user_id)
        .filter(PipelineJob.status == "paused")
        .distinct()
        .all()
    )
    return [int(row[0]) for row in rows]


async def watchdog_cycle() -> int:
    """One full watchdog sweep across all users with paused work.

    Returns the number of users drained. Safe to call from tests (single
    sweep, no loop) and from the periodic task below.
    """
    db = SessionLocal()
    total_drained = 0
    try:
        for user_id in users_with_paused_work(db):
            _state, drained = await check_and_drain(db, user_id)
            total_drained += drained
    finally:
        db.close()
    return total_drained


class AIWatchdog:
    """Periodic re-probe loop. Runs alongside the worker (API process or the
    standalone ``python -m app.worker``)."""

    def __init__(self, interval_seconds: Optional[float] = None):
        self.interval = (
            settings.ai_watchdog_interval_seconds
            if interval_seconds is None else interval_seconds
        )
        self.running = False

    @property
    def enabled(self) -> bool:
        """``AI_WATCHDOG_INTERVAL_SECONDS=0`` disables the automatic resume
        (paused work then only resumes via the one-click endpoint)."""
        return self.interval > 0

    async def run(self) -> None:
        if not self.enabled:
            log.info("AI watchdog disabled (AI_WATCHDOG_INTERVAL_SECONDS=0)")
            return
        self.running = True
        log.info("AI watchdog started (re-probe every %ss)", self.interval)
        while self.running:
            try:
                await watchdog_cycle()
            except Exception as exc:  # the watchdog must never die
                log.warning("AI watchdog cycle failed: %s", exc)
            await asyncio.sleep(max(1.0, self.interval))

    def stop(self) -> None:
        self.running = False
