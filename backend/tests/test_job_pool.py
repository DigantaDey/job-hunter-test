"""v2.3 — the shared job pool: cross-user coverage, 7-day retention, instant match.

The contract under test, in the order the product uses it:

* **Every run contributes.** A discovery run folds the postings it scanned into
  the pool (``record_candidates``), keyed by the same canonical identity the
  board uses, so a posting one user's adapters found is available to the next
  user's run *without* that user waiting for their own fan-out. A run that is
  offered pool candidates writes them back as contributions only if it actually
  fetched them — ``times_seen`` is a market signal, not a loop counter.
* **Retention is hard, and it is content that goes.** ``prune`` deletes entries
  past ``JOB_POOL_RETENTION_DAYS`` and folds them into ``job_pool_metrics``:
  counters, a SHA-256 identity, the public company name (the churn dimension)
  and nothing else — no title, no description, no URL, no payload. A *deleted*
  posting (a withdrawn listing) takes the same route immediately, which is the
  user's "deleted jobs keep aggregated metrics only" rule.
* **The owner console is the only reader.** The aggregates ride on
  ``GET /api/admin/overview``, behind the router-level ``require_owner``; a
  member gets 403 before the handler runs, and nothing on the user surface
  exposes them.
* **Onboarding matches instantly.** Completing onboarding (the extraction
  landing in ``profile_review_required``) fills the board from the pool with
  deterministic preliminary scores — zero network — and queues the user's own
  live discovery run behind it. The hook is idempotent per (profile, document)
  and never fails the onboarding step.
* **Off means off.** ``JOB_POOL_ENABLED=false`` reads nothing, writes nothing and
  queues nothing: the pre-pool behaviour, which is what a deployment that must
  not share postings between tenants needs.

Hermetic: the source fan-out is faked per test (the same double the discovery
suites use), AI is the deterministic conftest stand-in, and no test touches the
network.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

#: A description that overlaps the stand-in profile, so the deterministic
#: pre-rank has something to work with.
JD = ("Backend Engineer. Python, FastAPI and PostgreSQL services on AWS, packaged with "
      "Docker and orchestrated with Kubernetes. 5+ years building payments platforms at scale.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sync(db) -> None:
    """End any open read transaction so rows written by another session show up."""
    db.rollback()
    db.expire_all()


def _user(db, email: str = "owner@example.com"):
    from app.models.models import User

    _sync(db)
    user = db.query(User).filter(User.email == email).first()
    assert user is not None, f"no such user: {email}"
    return user


def _candidate(index: int, *, company: Optional[str] = None, source: str = "lever",
               hours_ago: float = 1.0) -> Dict[str, Any]:
    """A posting shaped the way discovery's candidates are (canonical + source keys)."""
    return {
        "title": f"Backend Engineer {index}",
        "company": company or f"Paystack {index}",
        "company_name_normalized": (company or f"Paystack {index}").lower(),
        "description": JD,
        "url": f"https://jobs.example.com/posting-{index}",
        "source": source,
        "external_id": f"ext-{source}-{index}",
        "dedupe_key": f"{source}:ext-{source}-{index}",
        "canonical_id": f"{source}:ext-{source}-{index}",
        "location": "Remote",
        "posted_at": datetime.utcnow() - timedelta(hours=hours_ago),
        "score": 55.0,
        "score_source": "preliminary",
    }


def _posting(index: int, *, source: str = "lever", hours_ago: float = 1.0):
    from app.services.sources.base import Posting

    return Posting(
        title=f"Backend Engineer {index}",
        company=f"Live Co {index}",
        url=f"https://live.example.com/{index}",
        source=source,
        external_id=f"live-{source}-{index}",
        location="Remote",
        description=JD,
        posted_at=datetime.utcnow() - timedelta(hours=hours_ago),
    )


def _install_fetch_all(monkeypatch, *, postings: Optional[List[Any]] = None,
                       requested: Optional[List[str]] = None) -> None:
    """Replace the registry fan-out with a fixed report (the discovery-suite double)."""
    from app.services import sources as registry

    returned = list(postings or [])
    ids = list(requested if requested is not None else sorted({p.source for p in returned}))
    report = {"requested": ids, "ok": {sid: len(returned) for sid in ids}, "errors": {},
              "skipped": {}, "total": len(returned)}

    async def fake_fetch_all(keywords, *, limit=40, since_hours=168, sources=None,
                             board_tokens=None, timeout_seconds=45.0):
        return list(returned), copy.deepcopy(report)

    monkeypatch.setattr(registry, "fetch_all", fake_fetch_all)


async def _discover(db, user, **kwargs) -> Dict[str, Any]:
    from app.services.discovery import discover_for_user

    params: Dict[str, Any] = {"keywords": ["python backend"], "freshness_hours": 48, "limit": 30,
                             "live_enabled": True, "source_ids": ["lever"]}
    params.update(kwargs)
    return await discover_for_user(db, user, **params)


# --------------------------------------------------------------------------- #
# 1. Every run contributes; the next user's run is offered what it found
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_run_contributes_its_postings_to_the_pool(client, auth, db, monkeypatch):
    """One user's fan-out is the pool's inventory — that is the coverage claim."""
    from app.models.models import JobPoolEntry

    _install_fetch_all(monkeypatch, postings=[_posting(1), _posting(2)])
    user = _user(db)
    report = await _discover(db, user)

    assert report["inserted"] == 2
    pool = report["pool"]
    assert pool["enabled"] is True and pool["retention_days"] == 7
    assert pool["ingested"] == 2, pool
    assert pool["live_entries"] == 2, pool
    assert db.query(JobPoolEntry).count() == 2

    # Canonical identity is the board's own key, so the two can merge.
    keys = {row.dedupe_key for row in db.query(JobPoolEntry).all()}
    assert "lever:live-lever-1" in keys


@pytest.mark.asyncio
async def test_a_second_users_run_is_offered_what_the_first_one_found(client, auth, member, db, monkeypatch):
    """The market another user already paid to fetch arrives on this user's board
    — with no adapter of their own returning it."""
    from app.models.models import Job

    _install_fetch_all(monkeypatch, postings=[_posting(1, source="greenhouse")])
    first = _user(db, "owner@example.com")
    await _discover(db, first, source_ids=["greenhouse"])

    # The second user's fan-out returns something else entirely.
    _install_fetch_all(monkeypatch, postings=[_posting(9, source="lever")])
    second = _user(db, "member@example.com")
    report = await _discover(db, second)

    assert report["pool"]["offered"] >= 1, report["pool"]
    titles = {row.title for row in db.query(Job).filter(Job.user_id == second.id).all()}
    assert "Backend Engineer 1" in titles, "the other user's find did not reach this board"
    assert "Backend Engineer 9" in titles


@pytest.mark.asyncio
async def test_pool_offered_candidates_are_not_re_contributed(client, auth, member, db, monkeypatch):
    """``times_seen`` counts the market's sightings, not a loop over the pool:

    a run that is handed two candidates *from* the pool must not bump their
    counters when it writes its scan back.
    """
    from app.models.models import JobPoolEntry

    _install_fetch_all(monkeypatch, postings=[_posting(1, source="greenhouse")])
    first = _user(db, "owner@example.com")
    await _discover(db, first, source_ids=["greenhouse"])
    before = {row.dedupe_key: row.times_seen for row in db.query(JobPoolEntry).all()}

    _install_fetch_all(monkeypatch, postings=[])
    second = _user(db, "member@example.com")
    await _discover(db, second, live_enabled=True)

    _sync(db)
    after = {row.dedupe_key: row.times_seen for row in db.query(JobPoolEntry).all()}
    assert after == before, "the pool's own candidates were counted as fresh sightings"


@pytest.mark.asyncio
async def test_pool_disabled_reads_nothing_writes_nothing(client, auth, db, monkeypatch):
    """``JOB_POOL_ENABLED=false`` is the pre-pool product, exactly."""
    from app.core.config import settings
    from app.models.models import JobPoolEntry

    monkeypatch.setattr(settings, "job_pool_enabled", False, raising=False)
    _install_fetch_all(monkeypatch, postings=[_posting(1)])
    report = await _discover(db, _user(db))

    assert report["inserted"] == 1, "discovery must still work with the pool off"
    assert "pool" not in report, "an absent key is the pre-pool report, byte for byte"
    assert db.query(JobPoolEntry).count() == 0


# --------------------------------------------------------------------------- #
# 2. Retention: content out, aggregates kept
# --------------------------------------------------------------------------- #
def test_retention_prunes_content_and_keeps_counters(db, owner):
    """Past the window the posting is gone; what remains answers market questions."""
    from app.models.models import JobPoolEntry, JobPoolMetric
    from app.services import job_pool

    user = _user(db)
    job_pool.record_candidates(db, [_candidate(1), _candidate(2)], user_id=user.id)
    # Age one entry past the window, leaving the other live.
    stale = db.query(JobPoolEntry).filter(JobPoolEntry.dedupe_key == "lever:ext-lever-1").one()
    stale.last_seen_at = datetime.utcnow() - timedelta(days=9)
    stale.expires_at = datetime.utcnow() - timedelta(days=2)
    db.commit()

    result = job_pool.prune(db)
    assert result["pruned"] == 1, result
    assert db.query(JobPoolEntry).filter(JobPoolEntry.dedupe_key == "lever:ext-lever-1").count() == 0
    assert job_pool.live_entry_count(db) == 1, "the live entry must survive"

    metric = db.query(JobPoolMetric).filter(JobPoolMetric.reason == "expired").one()
    assert metric.times_seen == 1 and metric.distinct_users == 1
    assert len(metric.metric_key) == 64, "identity is a hash, not a canonical key"


def test_a_deleted_posting_keeps_only_aggregates(db, owner):
    """A withdrawn listing takes the retention route immediately — the user's rule."""
    from app.models.models import JobPoolEntry, JobPoolMetric
    from app.services import job_pool

    user = _user(db)
    job_pool.record_candidates(db, [_candidate(1, company="Gone Ltd")], user_id=user.id)
    assert db.query(JobPoolEntry).count() == 1

    job_pool.record_posting_deleted(db, dedupe_key="lever:ext-lever-1",
                                    source="lever", company_name_normalized="gone ltd")
    assert db.query(JobPoolEntry).count() == 0, "the content must not outlive the posting"
    metric = db.query(JobPoolMetric).filter(JobPoolMetric.reason == "deleted").one()
    assert metric.times_seen == 1 and metric.company_name_normalized == "gone ltd"


def test_metric_rows_carry_no_job_content():
    """Schema-level pin: retention keeps counters, never the posting's content.

    If a future migration adds ``title``/``description``/``url``/``payload`` to
    ``job_pool_metrics``, this fails — the whole point of pruning is that the
    deleted job's content is not recoverable from the aggregate.
    """
    from app.models.models import JobPoolMetric

    columns = set(JobPoolMetric.__table__.columns.keys())
    forbidden = {"title", "description", "url", "external_id", "raw_payload", "extra",
                 "content_hash", "title_normalized", "dedupe_key", "user_id", "entry_id"}
    assert not (columns & forbidden), f"aggregate carries job content: {columns & forbidden}"


def test_metrics_snapshot_is_counters_only(db, owner):
    """The owner view is aggregates: what the pool holds, what it no longer holds."""
    from app.services import job_pool

    user = _user(db)
    job_pool.record_candidates(db, [_candidate(1), _candidate(2, company="Gone Ltd")], user_id=user.id)
    job_pool.record_posting_deleted(db, dedupe_key="lever:ext-lever-2",
                                    company_name_normalized="gone ltd")

    snapshot = job_pool.metrics_snapshot(db)
    assert snapshot["enabled"] is True and snapshot["retention_days"] == 7
    assert snapshot["live_entries"] == 1
    assert snapshot["contributors"] == 1
    assert snapshot["gone"]["all_time_by_reason"] == [
        {"reason": "deleted", "entries": 1, "times_seen": 1, "max_distinct_users": 1}
    ]
    # No posting content anywhere in the payload.
    blob = repr(snapshot)
    for forbidden in ("Backend Engineer", "jobs.example.com", "backend engineer"):
        assert forbidden not in blob, forbidden


def test_pool_stats_are_tenant_scoped_but_the_inventory_is_not(db, owner):
    """``stats`` takes a user (for board overlap); the entries themselves are shared."""
    from app.services import job_pool

    first = _user(db)
    job_pool.record_candidates(db, [_candidate(1)], user_id=first.id)
    stats = job_pool.stats(db, user_id=first.id)
    assert stats["enabled"] is True and stats["live_entries"] == 1


@pytest.mark.asyncio
async def test_a_closed_posting_is_withdrawn_from_the_pool_immediately(client, auth, member, db, monkeypatch):
    """A source marking a role closed is a deletion, not an expiry-by-age:

    the pool entry's content goes **now** (no waiting for retention) and the
    aggregate row records the deletion — what the owner console reports as churn.
    """
    from app.models.models import JobPoolEntry, JobPoolMetric

    posting = _posting(4, source="greenhouse")
    _install_fetch_all(monkeypatch, postings=[posting])
    first = _user(db, "owner@example.com")
    await _discover(db, first, source_ids=["greenhouse"])
    assert db.query(JobPoolEntry).count() == 1

    # The same canonical posting, now marked closed by its source.
    posting.expired = True
    _install_fetch_all(monkeypatch, postings=[posting])
    second = _user(db, "member@example.com")
    report = await _discover(db, second, source_ids=["greenhouse"])

    assert report["expired_dropped"] == 1, report
    assert db.query(JobPoolEntry).count() == 0, "a dead listing must not stay in the pool"
    metric = db.query(JobPoolMetric).filter(JobPoolMetric.reason == "deleted").one()
    assert metric.times_seen == 1


# --------------------------------------------------------------------------- #
# 3. Instant match: a fresh board before the first fetch returns
# --------------------------------------------------------------------------- #
def test_instant_match_fills_a_fresh_board_from_the_pool(db, owner, member):
    """Deterministic, honestly labelled, and bounded by the board's own cap."""
    from app.models.models import Job
    from app.services import job_pool

    contributor = _user(db)
    job_pool.record_candidates(db, [_candidate(i, source="greenhouse") for i in range(1, 4)],
                               user_id=contributor.id)
    fresh = _user(db, "member@example.com")

    report = job_pool.instant_match(db, fresh, profile=None, keywords=["python"])
    assert report["skipped"] is None, report
    assert report["inserted"] == 3, report
    rows = db.query(Job).filter(Job.user_id == fresh.id).all()
    assert len(rows) == 3
    assert {row.score_source for row in rows} == {"preliminary"}
    assert all((row.extra or {}).get("job_pool") for row in rows), "the pool origin is recorded"


def test_instant_match_is_idempotent_per_profile_document(db, owner, member):
    """The hook runs on the review path, so re-entry must be free — and correct.

    The first call matches and writes the marker; the second is a no-op. When
    the *document* changes (a reviewed field) the hook looks again — and because
    the board's own identity merges the row, "again" means zero new jobs, not
    duplicates.
    """
    from app.models.models import CandidateProfile, Job
    from app.services import job_pool

    job_pool.record_candidates(db, [_candidate(1)], user_id=_user(db).id)
    fresh = _user(db, "member@example.com")
    profile = _seed_profile(db, fresh)
    db.commit()

    first = job_pool.maybe_instant_match(db, fresh, trigger="onboarding")
    assert first is not None and first["inserted"] == 1, first
    assert db.query(Job).filter(Job.user_id == fresh.id).count() == 1

    again = job_pool.maybe_instant_match(db, fresh, trigger="onboarding")
    assert again is None, "an unchanged document must not match twice"

    # A corrected document is a new (profile, document) pair: looked at again,
    # but the pool entry is already on the board — nothing_new, no duplicate.
    profile.document = {**(profile.document or {}), "skills": ["Python", "FastAPI", "AWS"]}
    db.add(profile)
    db.commit()
    changed = job_pool.maybe_instant_match(db, fresh, trigger="profile_active")
    assert changed is not None and changed["inserted"] == 0, changed
    # ``pool_empty`` (nothing left in the pool for this user) and ``nothing_new``
    # (candidates offered, all already on the board) are both honest refusals;
    # what matters is that neither inserts a duplicate.
    assert changed["skipped"] in ("pool_empty", "nothing_new"), changed
    assert db.query(Job).filter(Job.user_id == fresh.id).count() == 1


def test_instant_match_respects_the_jobs_cap(db, owner, member, monkeypatch):
    """A full board is a refusal (``skipped``), never an exception."""
    from app.core import entitlements
    from app.models.models import Job
    from app.services import job_pool

    job_pool.record_candidates(db, [_candidate(1)], user_id=_user(db).id)
    user = _user(db, "member@example.com")
    monkeypatch.setattr(entitlements, "check_storage",
                        lambda db_, uid, key: (False, 25, 25, "Board is full"))

    report = job_pool.instant_match(db, user, profile=None)
    assert report["skipped"] == "jobs_cap_reached", report
    assert report["inserted"] == 0
    assert report["jobs_cap"] == {"used": 25, "limit": 25}
    assert db.query(Job).filter(Job.user_id == user.id).count() == 0


def test_instant_match_queues_the_live_run(client, auth, owner, member, db):
    """The other half of the promise: the pool matches *now*, the fetch runs after."""
    from app.models.models import PipelineJob
    from app.services import job_pool

    contributor = _user(db)
    job_pool.record_candidates(db, [_candidate(1)], user_id=contributor.id)
    fresh = _user(db, "member@example.com")
    _seed_profile(db, fresh)
    db.commit()

    report = job_pool.maybe_instant_match(db, fresh, trigger="onboarding", keywords=["python"])
    assert report is not None and report["inserted"] == 1, report
    assert report["discovery_queued"] is True
    queued = (db.query(PipelineJob)
              .filter(PipelineJob.user_id == fresh.id, PipelineJob.pipeline == "discovery")
              .one())
    assert queued.payload["trigger"] == "instant_match"
    assert queued.payload["keywords"] == ["python"]

    # And the marker makes a second call a no-op.
    again = job_pool.maybe_instant_match(db, fresh, trigger="onboarding", keywords=["python"])
    assert again is None, "the marker must stop a second match for the same document"


def test_maybe_instant_match_after_review_only_fires_on_an_active_profile(db, owner, member):
    """The review hook is inert until review actually finishes."""
    from app.models.models import Job
    from app.services import job_pool

    contributor = _user(db)
    job_pool.record_candidates(db, [_candidate(1)], user_id=contributor.id)
    fresh = _user(db, "member@example.com")
    profile = _seed_profile(db, fresh, state="review_required")
    db.commit()

    assert job_pool.maybe_instant_match_after_review(db, fresh, profile.id) is None
    assert db.query(Job).filter(Job.user_id == fresh.id).count() == 0


# --------------------------------------------------------------------------- #
# 4. Onboarding completion runs the hook (end to end)
# --------------------------------------------------------------------------- #
def test_onboarding_completion_matches_the_pool_and_queues_the_live_run(client, auth, owner, db):
    """The user's ask, end to end: finish onboarding → jobs on the board now,
    live discovery processing behind them."""
    from app.models.models import Job, PipelineJob, ResumeExtraction
    from app.services import job_pool
    from tests.conftest import pdf_bytes, run_queue_item

    contributor = _user(db)
    job_pool.record_candidates(db, [_candidate(i, source="greenhouse") for i in range(1, 4)],
                               user_id=contributor.id)

    upload = client.post("/api/onboarding/resume",
                         files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
                         headers=auth)
    assert upload.status_code == 200, upload.text
    extraction_id = upload.json()["extraction"]["id"]

    extraction = db.query(ResumeExtraction).filter(ResumeExtraction.id == extraction_id).one()
    run_queue_item(int(extraction.pipeline_job_id), "extraction")

    _sync(db)
    board = db.query(Job).filter(Job.user_id == contributor.id, Job.dedupe_key.like("greenhouse:%")).all()
    assert len(board) == 3, "the pool's matches did not land on the board"
    assert {row.score_source for row in board} == {"preliminary"}

    queued = (db.query(PipelineJob)
              .filter(PipelineJob.user_id == contributor.id, PipelineJob.pipeline == "discovery")
              .all())
    assert queued, "the live discovery run must be queued behind the instant match"
    assert any((item.payload or {}).get("trigger") == "instant_match" for item in queued)

    # A replayed extraction (the queue's idempotent replay) must not re-match.
    run_queue_item(int(extraction.pipeline_job_id), "extraction")
    _sync(db)
    assert db.query(Job).filter(Job.user_id == contributor.id).count() == 3


def test_profile_review_hook_survives_a_profile_that_is_not_active(client, auth, owner, db):
    """The review endpoints call the hook after every decision; a profile still in
    review must simply be skipped — never an error on the response."""
    from app.models.models import ResumeExtraction
    from tests.conftest import pdf_bytes, run_queue_item

    upload = client.post("/api/onboarding/resume",
                         files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
                         headers=auth)
    extraction_id = upload.json()["extraction"]["id"]
    extraction = db.query(ResumeExtraction).filter(ResumeExtraction.id == extraction_id).one()
    run_queue_item(int(extraction.pipeline_job_id), "extraction")

    review = client.get("/api/profile/review", headers=auth).json()
    field = next((item for item in review["fields"] if item.get("review_required")), None)
    if field is None:  # pragma: no cover - the stand-in extraction always leaves one
        pytest.skip("the stand-in extraction left nothing to review")
    response = client.post("/api/profile/review/resolve",
                           json={"profile_id": review["profile_id"], "path": field["path"],
                                 "action": "confirm"},
                           headers=auth)
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 5. The owner console is the only reader
# --------------------------------------------------------------------------- #
def test_pool_metrics_are_on_the_owner_overview(client, auth, owner, db):
    from app.services import job_pool

    job_pool.record_candidates(db, [_candidate(1)], user_id=_user(db).id)
    body = client.get("/api/admin/overview", headers=auth).json()
    assert body["job_pool"]["enabled"] is True
    assert body["job_pool"]["live_entries"] == 1
    assert "gone" in body["job_pool"]


def test_pool_metrics_are_not_reachable_by_a_member(client, member_auth):
    response = client.get("/api/admin/overview", headers=member_auth)
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Fixtures used by the service-level tests
# --------------------------------------------------------------------------- #
def _seed_profile(db, user, *, state: str = "active"):
    """A current candidate profile for ``user`` (the instant match needs one)."""
    from app.models.models import CandidateProfile

    profile = CandidateProfile(user_id=user.id, version=1, state=state, is_current=True,
                               document={"skills": ["Python", "FastAPI", "PostgreSQL"],
                                         "experience": [{"title": "Backend Engineer",
                                                         "company": "Paystack"}]},
                               completeness={"percent": 80})
    db.add(profile)
    db.flush()
    return profile


