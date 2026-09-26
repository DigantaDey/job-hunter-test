"""Database pool sizing + background DB work off the event loop (reported bug).

The report: a browser **hard refresh** burst drove the engine to

    QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00

— the same error hit ``worker claim failed``, ``AI watchdog cycle failed`` and
``lease heartbeat for item 8 failed`` — and while those waits ran *on the
shared event loop*, no HTTP request could complete until each 30-second wait
gave up. Two regressions are pinned here:

1. **The pool is the size the settings say it is.** ``DB_POOL_SIZE`` /
   ``DB_MAX_OVERFLOW`` / ``DB_POOL_RECYCLE`` / ``DB_POOL_TIMEOUT`` used to be
   applied only on the non-SQLite branch of ``_engine_kwargs()``, so the
   default file-SQLite deployment silently ran SQLAlchemy's own ``5 + 10``
   defaults no matter what ``.env`` said — even though ``.env.example``
   documents the settings directly under the SQLite default.
2. **The worker's periodic DB calls never block the event loop.** Claim, the
   lease-heartbeat renew, the watchdog's first query, the reaper and the
   user-action sweep each run in a worker thread: a saturated pool stalls one
   background call instead of every in-flight HTTP response.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

import app.db as db_module
import app.services.ai_watchdog as watchdog
import app.worker as worker_module
from app.core.config import settings
from app.models.models import User
from app.services.job_queue import enqueue
from app.worker import Worker


def _user(db) -> User:
    return db.query(User).order_by(User.id).first()


# --------------------------------------------------------------------------- #
# 1. Pool sizing follows the settings (file SQLite included)
# --------------------------------------------------------------------------- #
def test_file_sqlite_engine_receives_the_configured_pool_arguments(monkeypatch):
    """The reported bug: the sizing settings never reached a SQLite engine.

    A file-based SQLite URL must get the full pool shape — the error's own
    "size 5 overflow 10 ... timeout 30.00" was SQLAlchemy's default because
    ``_engine_kwargs()`` skipped every pool argument on the SQLite branch.
    """
    monkeypatch.setattr(db_module, "IS_SQLITE", True)
    monkeypatch.setattr(settings, "database_url", "sqlite:///./jobhunter.db")
    kwargs = db_module._engine_kwargs()
    assert kwargs["pool_size"] == settings.db_pool_size
    assert kwargs["max_overflow"] == settings.db_max_overflow
    assert kwargs["pool_recycle"] == settings.db_pool_recycle
    assert kwargs["pool_timeout"] == settings.db_pool_timeout
    assert kwargs["connect_args"] == {"check_same_thread": False, "timeout": 30}


def test_file_sqlite_actually_builds_a_sized_queue_pool(tmp_path, monkeypatch):
    """The configured arguments must be *accepted* by ``create_engine``.

    File SQLite runs on ``QueuePool`` (the reported error proves it), so pool
    sizing is valid there — and the built engine is the configured size, not
    SQLAlchemy's 5 + 10.
    """
    monkeypatch.setattr(db_module, "IS_SQLITE", True)
    url = f"sqlite:///{tmp_path / 'pool.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    engine = create_engine(url, **db_module._engine_kwargs())
    try:
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == settings.db_pool_size
        assert engine.pool._max_overflow == settings.db_max_overflow
    finally:
        engine.dispose()


def test_in_memory_sqlite_keeps_its_single_connection_pool(monkeypatch):
    """In-memory SQLite runs on a single-connection pool that *rejects*
    sizing arguments — the one URL the new code must keep excluding."""
    monkeypatch.setattr(db_module, "IS_SQLITE", True)
    for url in ("sqlite:///:memory:", "sqlite://"):
        monkeypatch.setattr(settings, "database_url", url)
        kwargs = db_module._engine_kwargs()
        assert "pool_size" not in kwargs, url
        assert "max_overflow" not in kwargs, url
        assert "pool_timeout" not in kwargs, url
        assert kwargs["connect_args"] == {"check_same_thread": False, "timeout": 30}
    # And a real engine from those kwargs constructs without an ArgumentError.
    memory_engine = create_engine("sqlite:///:memory:", **db_module._engine_kwargs())
    memory_engine.dispose()


def test_other_dialects_still_receive_the_configured_pool_arguments(monkeypatch):
    """PostgreSQL (the production target) keeps exactly the pool shape it had."""
    monkeypatch.setattr(db_module, "IS_SQLITE", False)
    kwargs = db_module._engine_kwargs()
    assert kwargs["pool_size"] == settings.db_pool_size
    assert kwargs["max_overflow"] == settings.db_max_overflow
    assert kwargs["pool_recycle"] == settings.db_pool_recycle
    assert kwargs["pool_timeout"] == settings.db_pool_timeout
    assert "connect_args" not in kwargs


def test_the_live_engine_pool_matches_the_settings():
    """The engine every request and worker session rides is sized by the
    settings (file SQLite in the test environment), not by the 5 + 10 default
    that produced the reported ``QueuePool limit ... timeout 30.00``."""
    assert isinstance(db_module.engine.pool, QueuePool)
    assert db_module.engine.pool.size() == settings.db_pool_size
    assert db_module.engine.pool._max_overflow == settings.db_max_overflow
    assert db_module.engine.pool._timeout == settings.db_pool_timeout


# --------------------------------------------------------------------------- #
# 2. Background DB work runs in worker threads, never on the event loop
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_worker_claim_runs_off_the_event_loop(db, monkeypatch):
    """A claim waits up to ``DB_POOL_TIMEOUT`` for a free connection when the
    pool is saturated — on the loop that froze every in-flight HTTP response
    for the full 30 seconds (the reported dead window). It must run in a
    thread; the loop slot itself must keep serving."""
    loop_thread = threading.current_thread()
    seen: dict = {}

    def probe_claim(_db, *, pipelines=None, **kwargs):
        seen["thread"] = threading.current_thread()
        return None

    monkeypatch.setattr(worker_module, "claim", probe_claim)

    worker = Worker(pipelines=["discovery"], concurrency=1, poll_interval=0.05)
    worker.running = True
    loop_task = asyncio.create_task(worker._loop())
    try:
        deadline = time.monotonic() + 10.0
        while "thread" not in seen and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert "thread" in seen, "the loop never reached claim"
        assert seen["thread"] is not loop_thread, "claim blocked the event loop"
        assert not loop_task.done(), "the loop slot died instead of claiming"
    finally:
        worker.running = False
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task


@pytest.mark.asyncio
async def test_lease_heartbeat_renew_runs_off_the_event_loop(db, owner, monkeypatch):
    """The heartbeat is the third reported failure (``lease heartbeat for
    item 8 failed``): its renew is a fresh pool checkout every beat and must
    not block the loop while waiting for a connection."""
    loop_thread = threading.current_thread()
    seen: dict = {}

    def probe_renew(_db, _item, *, worker=None, **kwargs):
        seen["thread"] = threading.current_thread()
        return True

    monkeypatch.setattr(worker_module, "renew", probe_renew)
    # The interval is clamped to a 1s floor, so the first beat lands at ~1s.
    monkeypatch.setattr(settings, "worker_heartbeat_interval_seconds", 0.05, raising=False)

    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="hb-offloop:1")
    worker = Worker(pipelines=["discovery"])
    heartbeat_task = asyncio.create_task(worker._heartbeat_lease(item))
    try:
        deadline = time.monotonic() + 8.0
        while "thread" not in seen and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert "thread" in seen, "the heartbeat never beat"
        assert seen["thread"] is not loop_thread, "the renew blocked the event loop"
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


@pytest.mark.asyncio
async def test_watchdog_cycle_first_query_runs_off_the_event_loop(monkeypatch):
    """The watchdog's user listing is the cycle's first pool checkout — the
    second reported failure (``AI watchdog cycle failed``). It must wait for a
    connection in a thread; the connection it gets is then held for the rest
    of the cycle, so the probe and the drain reuse it without waiting again."""
    loop_thread = threading.current_thread()
    seen: dict = {}

    def probe_users(_db):
        seen["thread"] = threading.current_thread()
        return []

    monkeypatch.setattr(watchdog, "users_with_paused_work", probe_users)
    assert await watchdog.watchdog_cycle() == 0
    assert "thread" in seen, "the cycle never listed users"
    assert seen["thread"] is not loop_thread, "the watchdog's query blocked the event loop"
