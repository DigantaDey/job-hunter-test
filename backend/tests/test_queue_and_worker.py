"""Durable queue semantics + worker execution (crash safety, retries, dead-letter)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta

import pytest

import app.worker as worker_module
from app.core import metrics
from app.core.config import settings
from app.models.models import Job, PipelineJob, Resume, User
from app.services import handlers
from app.services.job_queue import (
    claim,
    complete,
    enqueue,
    fail,
    queue_stats,
    recover_stalled,
)
from app.worker import Worker
from tests.conftest import capture_json_logs


async def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _status_of(db, item_id: int) -> str:
    """Fresh status read (the worker commits from its own session)."""
    db.expire_all()
    return db.get(PipelineJob, item_id).status


def _user(db) -> User:
    return db.query(User).order_by(User.id).first()


def test_enqueue_and_dedupe(db, owner):
    user = _user(db)
    first = enqueue(db, user_id=user.id, pipeline="discovery", payload={"keywords": ["python"]},
                    dedupe_key="discovery:x")
    assert first and first.status == "queued"
    duplicate = enqueue(db, user_id=user.id, pipeline="discovery", payload={"keywords": ["python"]},
                        dedupe_key="discovery:x")
    assert duplicate is None
    assert db.query(PipelineJob).count() == 1


def test_enqueue_rejects_unknown_pipeline(db, owner):
    user = _user(db)
    with pytest.raises(ValueError):
        enqueue(db, user_id=user.id, pipeline="nope")


def test_claim_is_exclusive_and_priority_ordered(db, owner):
    user = _user(db)
    low = enqueue(db, user_id=user.id, pipeline="discovery", priority=7, dedupe_key="low")
    high = enqueue(db, user_id=user.id, pipeline="discovery", priority=1, dedupe_key="high")

    first = claim(db, pipelines=["discovery"])
    assert first.id == high.id  # priority wins over arrival order
    assert first.status == "processing"
    # v2.2.2: attempts counts handler *failures*, not claims — a claim takes a
    # lease and leaves the failure budget alone (was: == 1).
    assert first.attempts == 0
    assert first.lease_expires_at > datetime.utcnow()

    second = claim(db, pipelines=["discovery"])
    assert second.id == low.id
    assert claim(db, pipelines=["discovery"]) is None


def test_complete_and_stats(db, owner):
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="email", dedupe_key="e1")
    claim(db, pipelines=["email"])
    complete(db, item, result={"sent": True})
    assert item.status == "done"
    assert item.payload["result"] == {"sent": True}
    stats = queue_stats(db, user_id=user.id)
    assert stats["email"]["done"] == 1


def test_failure_retries_with_backoff_then_dead_letters(db, owner):
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="application", max_attempts=2, dedupe_key="a1")
    claim(db, pipelines=["application"])
    assert fail(db, item, "boom") == "retrying"
    assert item.status == "queued"
    assert item.scheduled_at > datetime.utcnow()

    # Make it runnable again and exhaust the failure budget.
    item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    claimed = claim(db, pipelines=["application"])
    # v2.2.2: one recorded failure so far, and the second claim adds nothing
    # (was: == 2, when claims incremented the counter).
    assert claimed.attempts == 1
    assert fail(db, claimed, "boom again") == "dead"
    assert claimed.status == "dead"
    assert claimed.attempts == 2, "max_attempts=2 → the second failure is the cap"


def test_non_retryable_failure_dead_letters_immediately(db, owner):
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", dedupe_key="ai1")
    claim(db, pipelines=["ai"])
    assert fail(db, item, "config invalid", retryable=False) == "dead"


def test_recover_stalled_items(db, owner):
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="stall")
    claim(db, pipelines=["discovery"])
    item.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
    db.commit()

    # v2.3: recover_stalled has a safety margin (default 300s) to avoid
    # cloning a live worker still inside a 300s AI call. For this unit test
    # we want immediate recovery, so pass safety_margin_seconds=0.
    assert recover_stalled(db, safety_margin_seconds=0) == 1
    db.refresh(item)
    assert item.status == "queued"
    assert item.locked_by == ""


@pytest.mark.asyncio
async def test_worker_executes_handler_end_to_end(db, client, auth, uploaded_resume, monkeypatch):
    """A worker run picks the item up, executes the real handler and completes it."""
    user = _user(db)
    resume = db.query(Resume).filter(Resume.user_id == user.id).first()

    async def fake_tag(profile, ai_config=None):
        return ["backend-python", "fintech"]

    monkeypatch.setattr("app.services.resume_generator.tag_resume", fake_tag)

    item = enqueue(db, user_id=user.id, pipeline="ai",
                   payload={"task": "tag_resume", "resume_id": resume.id}, dedupe_key=f"tag:{resume.id}")
    worker = Worker(pipelines=["ai"])
    await worker._run_item(item.id, "ai")

    db.refresh(item)
    assert item.status == "done"
    db.refresh(resume)
    assert resume.tags == ["backend-python", "fintech"]


@pytest.mark.asyncio
async def test_worker_marks_needs_input(db, client, auth, uploaded_resume, monkeypatch):
    user = _user(db)
    job = Job(user_id=user.id, title="Needs Input Role", company="Acme",
              description="Python", url="https://jobs.lever.co/acme/2", source="lever",
              dedupe_key="ni:1", score=80.0,
              extra={"forms": {"portal_type": "custom", "detection_source": "html", "requires_login": False,
                               "fields": [{"name": "fav_color", "label": "Favourite colour", "type": "text",
                                           "required": True, "options": [], "profile_key": None}]}})
    db.add(job)
    db.commit()
    db.refresh(job)

    item = enqueue(db, user_id=user.id, pipeline="application", job_id=job.id, dedupe_key=f"apply:{job.id}")
    worker = Worker(pipelines=["application"])
    await worker._run_item(item.id, "application")

    db.refresh(item)
    assert item.status == "needs_input"


@pytest.mark.asyncio
async def test_worker_records_failures_without_crashing(db, owner, monkeypatch):
    user = _user(db)

    async def broken(db_session, item):
        raise RuntimeError("upstream exploded")

    monkeypatch.setitem(handlers.HANDLERS, "discovery", broken)

    item = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="broken")
    worker = Worker(pipelines=["discovery"])
    await worker._run_item(item.id, "discovery")

    db.refresh(item)
    assert item.status == "queued"  # retryable → back to the queue
    assert "upstream exploded" in item.error


@pytest.mark.asyncio
async def test_worker_logs_the_queue_users_id_as_a_number(db, owner, monkeypatch):
    """
    The worker binds the item's user to ``LogContext``, so its own records read
    the id off the ContextVar rather than passing it in ``extra=``. In JSON mode
    that path used to emit ``"7"`` while the access log emitted ``7`` — one
    field, two types. The queue user is an ``int`` and must stay one.
    """
    async def ok_handler(db_session, item):
        return {"ok": True}

    monkeypatch.setitem(handlers.HANDLERS, "discovery", ok_handler)

    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="discovery",
                   payload={"task": "noop"}, dedupe_key="json-user-id")
    worker = Worker(pipelines=["discovery"])
    with capture_json_logs("app.worker") as sink:
        await worker._run_item(item.id, "discovery")

    processing = [p for p in sink.payloads if str(p.get("message", "")).startswith("processing")]
    assert processing, f"the worker did not log its item: {sink.payloads}"
    assert isinstance(processing[0]["user_id"], int), processing[0]
    assert processing[0]["user_id"] == user.id


def test_worker_ignores_disabled_pipelines():
    worker = Worker(pipelines=["discovery", "not-a-pipeline"])
    assert worker.pipelines == ["discovery"]


# --------------------------------------------------------------------------- #
# v2.1.1 — self-healing worker + honest (durable) AI queue
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_worker_loop_survives_handler_crash_and_drains_next_item(db, owner, monkeypatch):
    """A plain (non-AI) RuntimeError dead-letters the item per fail() semantics,
    and the SAME loop task keeps serving: it drains the next enqueued item to
    completion (loop liveness)."""
    user = _user(db)

    async def fail_once(db_session, item):
        if (item.payload or {}).get("task") == "boom":
            raise RuntimeError("upstream exploded")
        return {"ok": True}

    monkeypatch.setitem(handlers.HANDLERS, "discovery", fail_once)

    first = enqueue(db, user_id=user.id, pipeline="discovery", payload={"task": "boom"},
                    max_attempts=1, dedupe_key="boom-1")
    second = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="boom-2")

    worker = Worker(pipelines=["discovery"], concurrency=1, poll_interval=0.05)
    worker.running = True
    loop_task = asyncio.create_task(worker._loop())
    try:
        assert await _wait_until(lambda: _status_of(db, first.id) in ("dead", "queued"))
        # The same live loop task must drain the second item to done.
        assert await _wait_until(lambda: _status_of(db, second.id) == "done")
    finally:
        worker.running = False
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

    # Existing fail() semantics: attempts exhausted (1/1) → dead with the error.
    assert _status_of(db, first.id) == "dead"
    row = db.get(PipelineJob, first.id)
    assert "upstream exploded" in row.error
    second = db.get(PipelineJob, second.id)
    assert second.status == "done"
    assert second.payload.get("result") == {"ok": True}


@pytest.mark.asyncio
async def test_worker_loop_survives_complete_error_and_keeps_serving(db, owner, monkeypatch, caplog):
    """The handler returns, but ``complete()`` blows up OUTSIDE the protected
    block because the handler corrupted its own row. The new ``_loop`` guard
    catches it: the error is logged + counted (item_errors_total) and the SAME
    slot processes the next item — the corrupted row is left to the lease
    machinery."""
    user = _user(db)

    async def corrupt_then_ok(db_session, item):
        if (item.payload or {}).get("task") == "corrupt":
            item.payload = "corrupted-not-a-dict"
            db_session.commit()
        return {"ok": True}

    monkeypatch.setitem(handlers.HANDLERS, "discovery", corrupt_then_ok)

    first = enqueue(db, user_id=user.id, pipeline="discovery", payload={"task": "corrupt"},
                    dedupe_key="corrupt-1")
    second = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="corrupt-2")

    before = metrics.snapshot()
    worker = Worker(pipelines=["discovery"], concurrency=1, poll_interval=0.05)
    worker.running = True
    loop_task = asyncio.create_task(worker._loop())
    try:
        # The same slot must drain the second item to done despite the error.
        assert await _wait_until(lambda: _status_of(db, second.id) == "done")
    finally:
        worker.running = False
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

    err_key = 'jobhunter_worker_item_errors_total{pipeline="discovery"}'
    assert metrics.snapshot().get(err_key, 0) - before.get(err_key, 0) == 1
    # The loop's own logging captured the escaped error.
    assert any("escaped _run_item" in r.getMessage() for r in caplog.records)
    # The corrupted row is left in `processing` for the lease machinery
    # (recover_stalled re-queues it once the lease expires).
    assert _status_of(db, first.id) == "processing"
    assert db.get(PipelineJob, first.id).payload == "corrupted-not-a-dict"


@pytest.mark.asyncio
async def test_supervisor_logs_and_respawns_crashed_loop_task(db, owner, monkeypatch, caplog):
    """A slot that dies with an exception (outside every protected block) is
    logged once with its traceback, counted, and respawned — subsequent items
    are still processed. stop() suppresses respawns."""
    user = _user(db)

    # Keep pytest's caplog handler alive: start() normally rewires the root
    # logging handlers, which would wipe the capture.
    monkeypatch.setattr(worker_module, "configure_logging", lambda: None)
    caplog.set_level(logging.ERROR, logger="app.worker")

    async def ok_handler(db_session, item):
        return {"ok": True}

    monkeypatch.setitem(handlers.HANDLERS, "discovery", ok_handler)

    # The DB-teardown class of failure: the SECOND SessionLocal() call in the
    # worker process raises — call 1 is startup recovery, call 2 is the first
    # iteration of the (single) loop slot, outside every try/except in _loop.
    real_session_local = worker_module.SessionLocal
    calls = {"n": 0}

    def flaky_session_local():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database connection pool exhausted (simulated)")
        return real_session_local()

    monkeypatch.setattr(worker_module, "SessionLocal", flaky_session_local)

    item = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="respawn-1")
    before = metrics.snapshot()

    worker = Worker(pipelines=["discovery"], concurrency=1, poll_interval=0.05)
    start_task = asyncio.create_task(worker.start())
    try:
        crash_key = 'jobhunter_worker_task_crashes_total{task="loop-0"}'
        # 1) The slot dies and the supervisor records the crash.
        assert await _wait_until(
            lambda: metrics.snapshot().get(crash_key, 0) - before.get(crash_key, 0) >= 1)
        assert worker._last_crash is not None
        assert worker._last_crash["task"] == "loop-0"
        assert "database connection pool exhausted" in worker._last_crash["error"]
        # 2) A fresh task replaced the dead slot.
        assert await _wait_until(
            lambda: worker._tasks.get("loop-0") is not None and not worker._tasks["loop-0"].done())
        # 3) Subsequent items are processed to completion.
        assert await _wait_until(lambda: _status_of(db, item.id) == "done")
    finally:
        await worker.stop()
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task

    # The crash was logged exactly once, with the traceback.
    crash_records = [r for r in caplog.records if "worker task 'loop-0' crashed" in r.getMessage()]
    assert len(crash_records) == 1
    assert crash_records[0].exc_info is not None
    assert "database connection pool exhausted (simulated)" in caplog.text
    # Exactly one crash — the flaky DB call fired once.
    assert metrics.snapshot().get(crash_key, 0) - before.get(crash_key, 0) == 1


@pytest.mark.asyncio
async def test_stalled_processing_item_recovered_at_restart_and_completed(db, owner):
    """Restart durability: a row forced to `processing` with an expired lease
    (worker crash) is returned to the queue by startup recovery, then the
    worker completes it — exactly once, no duplicates."""
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "noop"},
                   dedupe_key="stall-recover")
    claimed = claim(db, pipelines=["ai"])
    assert claimed.id == item.id

    # Simulate the worker process dying mid-run: expired lease, dead owner.
    db.expire_all()
    stalled = db.get(PipelineJob, item.id)
    stalled.lease_expires_at = datetime.utcnow() - timedelta(seconds=30)
    stalled.locked_by = "dead-worker-42"
    db.commit()

    # "Restart": Worker.start() recovers before serving (as does the API
    # lifespan) — the row is back in the queue.
    # v2.3: startup recovery uses the same safety margin (300s) to avoid
    # cloning a live worker on rolling deploy. For this test we set the
    # worker's safety to 0 so the 30s-expired row is recovered immediately.
    worker = Worker(pipelines=["ai"], poll_interval=0.05, reaper_safety_seconds=0)
    await worker._recover()
    assert _status_of(db, item.id) == "queued"

    worker.running = True
    loop_task = asyncio.create_task(worker._loop())
    try:
        assert await _wait_until(lambda: _status_of(db, item.id) == "done")
    finally:
        worker.running = False
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

    # Completed — with exactly one row (no duplicates).
    db.expire_all()
    rows = db.query(PipelineJob).filter(PipelineJob.dedupe_key == "stall-recover").all()
    assert len(rows) == 1
    assert rows[0].status == "done"
    assert rows[0].payload.get("result") == {"status": "noop", "user": user.id}


def test_ai_queue_stats_empty_contract(db, client, auth):
    """With no AI work, the stats endpoint reports an honest empty queue."""
    user = _user(db)
    assert user is not None
    data = client.get("/api/pipelines/stats", headers=auth).json()
    assert data["ai"] == {"queued": 0, "processing": 0, "done": 0, "failed": 0,
                          "needs_input": 0, "dead": 0, "paused": 0}
    assert data["ai_queue"] == {"queue_preview": [], "processing_now": None}


@pytest.mark.asyncio
async def test_ai_queue_lifecycle_queued_processing_done(db, client, auth, uploaded_resume, monkeypatch):
    """The ai_queue block tracks a real tag_resume item through the durable
    queue: queued → (claimed) processing_now → done, draining to empty."""
    user = _user(db)
    resume = db.query(Resume).filter(Resume.user_id == user.id).first()
    assert resume is not None

    async def fake_tag(profile, ai_config=None):
        return ["backend-python", "fintech"]

    monkeypatch.setattr("app.services.resume_generator.tag_resume", fake_tag)

    # The upload itself enqueued the tag_resume item — it is already visible.
    item = db.query(PipelineJob).filter(PipelineJob.dedupe_key == f"tag_resume:{resume.id}").first()
    assert item is not None
    assert item.status == "queued"

    data = client.get("/api/pipelines/stats", headers=auth).json()
    assert data["ai"]["queued"] == 1
    assert data["ai_queue"]["queue_preview"] == [
        {"id": item.id, "workflow": "tag_resume", "priority": 4, "status": "queued"}]
    assert data["ai_queue"]["processing_now"] is None

    # The worker claims it — processing_now reflects the real row.
    claimed = claim(db, pipelines=["ai"])
    assert claimed.id == item.id
    data = client.get("/api/pipelines/stats", headers=auth).json()
    assert data["ai"]["processing"] == 1
    assert data["ai_queue"]["processing_now"] == {"id": item.id, "workflow": "tag_resume"}

    # The worker completes it — the view drains.
    worker = Worker(pipelines=["ai"])
    await worker._run_item(item.id, "ai")
    data = client.get("/api/pipelines/stats", headers=auth).json()
    assert data["ai"]["done"] == 1
    assert data["ai_queue"]["processing_now"] is None
    assert data["ai_queue"]["queue_preview"] == []

    db.expire_all()
    assert db.get(Resume, resume.id).tags == ["backend-python", "fintech"]


def test_ai_queue_preview_orders_by_priority_and_caps_at_three(db, client, auth):
    """queue_preview is the top 3 in-flight items by (priority, created_at)."""
    user = _user(db)
    low = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": 1},
                  priority=9, dedupe_key="aq-low")
    mid = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": 1},
                  priority=3, dedupe_key="aq-mid")
    default = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": 1},
                      dedupe_key="aq-default")
    high = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": 1},
                   priority=1, dedupe_key="aq-high")

    data = client.get("/api/pipelines/stats", headers=auth).json()
    preview = data["ai_queue"]["queue_preview"]
    assert [p["id"] for p in preview] == [high.id, mid.id, default.id]
    assert low.id not in [p["id"] for p in preview]
    assert preview[0] == {"id": high.id, "workflow": "tag_resume", "priority": 1, "status": "queued"}
    assert preview[2]["priority"] == 5


def test_ops_status_reports_worker_tasks(client, auth, owner):
    """GET /api/ops/status exposes workers.tasks (expected/alive/last_crash)."""
    from app.main import app as fastapi_app

    assert getattr(fastapi_app.state, "worker", None) is None
    data = client.get("/api/ops/status", headers=auth).json()
    assert data["workers"]["tasks"] == {
        "expected": settings.worker_concurrency, "alive": 0, "last_crash": None}

    worker = Worker(pipelines=["ai"], concurrency=2, poll_interval=0.05)
    fastapi_app.state.worker = worker
    try:
        data = client.get("/api/ops/status", headers=auth).json()
        assert data["workers"]["tasks"]["expected"] == 2
        assert data["workers"]["tasks"]["alive"] == 0  # not started yet
        assert data["workers"]["tasks"]["last_crash"] is None
    finally:
        fastapi_app.state.worker = None
