"""Durable queue semantics + worker execution (crash safety, retries, dead-letter)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

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
    assert first.attempts == 1
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

    # Make it runnable again and exhaust the attempts.
    item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    claimed = claim(db, pipelines=["application"])
    assert claimed.attempts == 2
    assert fail(db, claimed, "boom again") == "dead"
    assert claimed.status == "dead"


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

    assert recover_stalled(db) == 1
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


def test_worker_ignores_disabled_pipelines():
    worker = Worker(pipelines=["discovery", "not-a-pipeline"])
    assert worker.pipelines == ["discovery"]
