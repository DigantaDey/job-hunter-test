"""v2.4 — where the seconds go, and the lease that protects a long run.

Seven things this file pins, in the order they were reported:

1. **The lease is renewed while the handler runs** (``worker._heartbeat_lease``
   → ``job_queue.renew``), so ``recover_stalled`` can no longer clone a slow
   discovery run — and when a row *is* reclaimed, ``complete`` is a
   compare-and-set that refuses the loser's outcome instead of overwriting the
   winner's. ``WORKER_MAX_RUNTIME_SECONDS`` is a real soft deadline now.
2. **Every run reports its own stage timings** (``report["stages"]``) — fetch,
   pool, pre-rank, the scoring slice, the classification slice, persistence,
   form detection — measured, never inferred.
3. **Scoring and classification are one wave**, under one per-run AI budget
   (``DISCOVERY_AI_BUDGET_SECONDS``); whatever the budget cuts keeps the honest
   ``preliminary`` verdict it already carried, and the run says so.
4. **A board source's fan-out is non-destructive**: the boards that answered are
   kept and the source is reported ``partial`` with the counts, instead of the
   whole source being discarded as ``timeout``.
5. **Search discovery is bounded-parallel** (queries and the validation crawl)
   without touching the provider order, the page budget or the paid-attempt cap.
6. **Per-pipeline slot budgets** (``WORKER_PIPELINE_LIMITS``) keep one slow
   pipeline from taking every worker slot.
7. **The tail after persistence is bounded**: the board's events are one commit,
   and the funding flag is maintained from the run's own created companies with
   a targeted ``IN`` query instead of a full board scan.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

import app.services.job_queue as job_queue_module
from app.core import metrics
from app.core.config import settings
from app.models.models import FundingCompany, PipelineJob, User
from app.services import discovery as discovery_module
from app.services import handlers
from app.services.job_queue import (
    claim,
    claim_item,
    complete,
    enqueue,
    owns,
    pipeline_limits,
    recover_stalled,
    renew,
    worker_id,
)
from app.services.sources import Posting
from app.worker import Worker

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _user(db) -> User:
    return db.query(User).order_by(User.id).first()


def _snapshot() -> Dict[str, int]:
    return dict(metrics.snapshot())


def _delta(before: Dict[str, int], key: str) -> int:
    return _snapshot().get(key, 0) - before.get(key, 0)


def _status_of(db, item_id: int) -> str:
    db.expire_all()
    return db.get(PipelineJob, item_id).status


def _candidate(index: int, description: str = "Backend engineer. Python, FastAPI, PostgreSQL.") -> Dict[str, Any]:
    return {
        "title": f"Backend Engineer {index}",
        "company": f"Acme {index}",
        "url": f"https://jobs.example.com/{index}",
        "source": "lever",
        "external_id": f"ext-{index}",
        "description": description,
        "posted_at": datetime.utcnow() - timedelta(hours=index),
    }


def _install_fetch_all(monkeypatch, postings: List[Posting], *, delay: float = 0.0) -> None:
    """Deterministic source fan-out (discovery's only non-AI external boundary)."""
    from app.services import sources as registry

    async def fake_fetch_all(keywords, *, limit=40, since_hours=168, sources=None,
                             board_tokens=None, timeout_seconds=45.0):
        if delay:
            await asyncio.sleep(delay)
        report = {"requested": ["lever"], "ok": {"lever": len(postings)}, "errors": {},
                  "skipped": {}, "partial": {}, "error_codes": {}, "merged": 0,
                  "expired": 0, "total": len(postings)}
        return list(postings), report

    monkeypatch.setattr(registry, "fetch_all", fake_fetch_all)


def _posting(index: int) -> Posting:
    return Posting(title=f"Backend Engineer {index}", company=f"Northwind {index}",
                   url=f"https://jobs.example.com/posting-{index}", source="lever",
                   external_id=f"ext-{index}", location="Remote",
                   description="Python, FastAPI and PostgreSQL services on AWS.",
                   posted_at=datetime.utcnow() - timedelta(hours=index))


# --------------------------------------------------------------------------- #
# 1. The lease heartbeat, the CAS completion and the soft deadline
# --------------------------------------------------------------------------- #
def test_worker_identity_is_stable_for_the_process(db, owner, monkeypatch):
    """A per-call id would make every claim refuse its own heartbeat."""
    monkeypatch.setattr(settings, "worker_id", "", raising=False)
    assert worker_id() == worker_id()


def test_renew_extends_the_lease_only_while_the_row_is_still_ours(db, owner, monkeypatch):
    monkeypatch.setattr(settings, "worker_id", "", raising=False)
    user = _user(db)
    enqueue(db, user_id=user.id, pipeline="email", dedupe_key="renew:1")
    claimed = claim(db, pipelines=["email"], lease_seconds=30, limits={})
    assert claimed is not None
    first_lease = claimed.lease_expires_at
    assert first_lease is not None

    # A row held by someone else is not renewable — the CAS names the owner.
    assert renew(db, claimed, worker="some-other-worker", lease_seconds=600) is False
    db.refresh(claimed)
    assert claimed.lease_expires_at == first_lease

    # The owner's heartbeat extends it...
    assert renew(db, claimed, worker=worker_id(), lease_seconds=600) is True
    db.refresh(claimed)
    assert claimed.lease_expires_at > first_lease

    # ...and a finished row is never renewable again.
    assert complete(db, claimed) is True
    assert renew(db, claimed, worker=worker_id()) is False


def test_owns_is_a_read_only_probe_of_a_processing_row(db, owner, monkeypatch):
    monkeypatch.setattr(settings, "worker_id", "", raising=False)
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="email", dedupe_key="owns:1")
    claimed = claim(db, pipelines=["email"], lease_seconds=30, limits={})
    assert owns(db, claimed, worker=worker_id()) is True
    assert owns(db, claimed, worker="some-other-worker") is False
    job_queue_module.fail(db, claimed, "nope", retryable=True)
    assert owns(db, claimed, worker=worker_id()) is False, "a re-queued row has no owner"


@pytest.mark.asyncio
async def test_heartbeat_keeps_a_long_run_from_being_reclaimed(db, owner, monkeypatch):
    """The clone scenario: a handler that outlives its lease is not re-queued.

    ``WORKER_LEASE_SECONDS`` is 1 s here and the reaper runs with no safety
    margin, so without the heartbeat the row would be re-queued mid-run (the
    v2.3 behaviour that duplicated slow discovery runs). The heartbeat renews it
    every third of the lease, so the reaper finds nothing to recover.
    """
    user = _user(db)
    started, release = asyncio.Event(), asyncio.Event()
    finished = {"cancelled": False}

    async def slow_handler(db_session, item):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:  # pragma: no cover - this test never cancels
            finished["cancelled"] = True
            raise
        return {"ok": True}

    monkeypatch.setitem(handlers.HANDLERS, "email", slow_handler)
    monkeypatch.setattr(settings, "worker_lease_seconds", 1, raising=False)
    monkeypatch.setattr(settings, "worker_heartbeat_interval_seconds", 0.05, raising=False)
    monkeypatch.setattr(settings, "worker_max_runtime_seconds", 0, raising=False)

    item = enqueue(db, user_id=user.id, pipeline="email", dedupe_key="heartbeat:1")
    worker = Worker(pipelines=["email"])
    task = asyncio.create_task(worker._run_item(item.id, "email"))
    try:
        assert await asyncio.wait_for(started.wait(), 5.0)
        # 2.2 leases: without a heartbeat the row is long expired by now.
        await asyncio.sleep(2.2)
        assert recover_stalled(db, pipelines=["email"], safety_margin_seconds=0) == 0, \
            "the reaper cloned a run whose heartbeat was renewing its lease"
        db.expire_all()
        row = db.get(PipelineJob, item.id)
        assert row.status == "processing"
        assert row.locked_by == worker.worker_id
        assert row.lease_expires_at > datetime.utcnow()
    finally:
        release.set()
        await asyncio.wait_for(task, 10.0)

    assert finished["cancelled"] is False
    assert _status_of(db, item.id) == "done"


@pytest.mark.asyncio
async def test_a_reclaimed_row_is_not_completed_by_the_worker_that_lost_it(db, owner, monkeypatch):
    """The other half of the fix: the loser's outcome is discarded, not written.

    The heartbeat is disabled here to *simulate* the reclaim (a wedged process),
    the reaper re-queues the row, a second worker claims and finishes it — and
    then the original handler returns. Before v2.4 its ``complete()`` overwrote
    ``payload.result`` unconditionally.
    """
    user = _user(db)

    async def no_heartbeat(self, item):  # pragma: no cover - cancelled immediately
        await asyncio.sleep(3600)

    async def slow_handler(db_session, item):
        await release.wait()
        return {"winner": "the original worker"}

    started, release = asyncio.Event(), asyncio.Event()

    async def handler(db_session, item):
        started.set()
        return await slow_handler(db_session, item)

    monkeypatch.setitem(handlers.HANDLERS, "email", handler)
    monkeypatch.setattr(Worker, "_heartbeat_lease", no_heartbeat)
    monkeypatch.setattr(settings, "worker_lease_seconds", 1, raising=False)
    monkeypatch.setattr(settings, "worker_max_runtime_seconds", 0, raising=False)

    item = enqueue(db, user_id=user.id, pipeline="email", dedupe_key="loser:1")
    worker = Worker(pipelines=["email"])
    task = asyncio.create_task(worker._run_item(item.id, "email"))
    assert await asyncio.wait_for(started.wait(), 5.0)

    # The lease expires and the reaper re-queues the row.
    db.expire_all()
    row = db.get(PipelineJob, item.id)
    row.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
    db.commit()
    assert recover_stalled(db, pipelines=["email"], safety_margin_seconds=0) == 1

    # A *second* worker claims and finishes it first.
    monkeypatch.setattr(job_queue_module, "worker_id", lambda: "second-worker")
    other = claim_item(db, item.id, lease_seconds=60)
    assert other is not None and other.locked_by == "second-worker"
    before = _snapshot()
    assert complete(db, other, result={"winner": "the second worker"}) is True

    # Now the original worker's handler returns.
    release.set()
    await asyncio.wait_for(task, 10.0)

    db.expire_all()
    row = db.get(PipelineJob, item.id)
    assert row.status == "done"
    assert row.payload["result"] == {"winner": "the second worker"}, "the loser overwrote the winner"
    assert _delta(before, 'jobhunter_queue_complete_conflicts_total{pipeline="email"}') == 1
    assert _delta(before, 'jobhunter_queue_completed_total{pipeline="email"}') == 1, \
        "only the writer that actually finished the row counts as a completion"


@pytest.mark.asyncio
async def test_the_soft_deadline_cancels_a_wedged_handler_and_requeues_honestly(
        db, owner, monkeypatch):
    """``WORKER_MAX_RUNTIME_SECONDS`` used to be read by nothing at all."""
    user = _user(db)
    cancelled = {"flag": False}

    async def wedged(db_session, item):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise
        return {"never": True}  # pragma: no cover - the deadline fires first

    monkeypatch.setitem(handlers.HANDLERS, "email", wedged)
    monkeypatch.setattr(settings, "worker_max_runtime_seconds", 0.15, raising=False)

    item = enqueue(db, user_id=user.id, pipeline="email", dedupe_key="deadline:1")
    before = _snapshot()
    worker = Worker(pipelines=["email"])
    await asyncio.wait_for(worker._run_item(item.id, "email"), 10.0)

    assert cancelled["flag"] is True, "the handler was not cancelled at the deadline"
    db.expire_all()
    row = db.get(PipelineJob, item.id)
    assert row.status == "queued", "a run stopped at the deadline is re-queued, never left processing"
    assert "WORKER_MAX_RUNTIME_SECONDS" in row.error
    assert _delta(before, 'jobhunter_worker_item_deadline_total{pipeline="email"}') == 1


@pytest.mark.asyncio
async def test_a_worker_that_lost_its_lease_does_not_write_a_failure(db, owner, monkeypatch):
    """The failure path is fenced too: a reclaimed row is left to its owner."""
    user = _user(db)
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(db_session, item):
        started.set()
        await release.wait()
        raise RuntimeError("the run blew up after the reclaim")

    async def no_heartbeat(self, item):  # pragma: no cover - cancelled immediately
        await asyncio.sleep(3600)

    monkeypatch.setitem(handlers.HANDLERS, "email", handler)
    monkeypatch.setattr(Worker, "_heartbeat_lease", no_heartbeat)
    monkeypatch.setattr(settings, "worker_lease_seconds", 1, raising=False)
    monkeypatch.setattr(settings, "worker_max_runtime_seconds", 0, raising=False)

    item = enqueue(db, user_id=user.id, pipeline="email", dedupe_key="fenced:1")
    worker = Worker(pipelines=["email"])
    task = asyncio.create_task(worker._run_item(item.id, "email"))
    assert await asyncio.wait_for(started.wait(), 5.0)

    db.expire_all()
    row = db.get(PipelineJob, item.id)
    row.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
    db.commit()
    assert recover_stalled(db, pipelines=["email"], safety_margin_seconds=0) == 1
    monkeypatch.setattr(job_queue_module, "worker_id", lambda: "second-worker")
    assert claim_item(db, item.id, lease_seconds=60) is not None

    release.set()
    await asyncio.wait_for(task, 10.0)

    db.expire_all()
    row = db.get(PipelineJob, item.id)
    assert row.status == "processing", "the loser re-queued the row its new owner is running"
    assert row.locked_by == "second-worker"
    assert row.attempts == 0, "the loser spent the failure budget of a run it no longer owned"
    assert "blew up" not in (row.error or "")


# --------------------------------------------------------------------------- #
# 2. Stage timings
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_every_run_reports_a_per_stage_wall_clock(db, owner, monkeypatch):
    user = _user(db)
    _install_fetch_all(monkeypatch, [_posting(0), _posting(1)], delay=0.05)

    report = await discovery_module.discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["lever"],
    )

    assert report["inserted"] == 2
    stages = report["stages"]
    assert set(stages) == {"fetch_ms", "pool_ms", "pre_rank_ms", "score_ms",
                           "classify_ms", "persist_ms", "forms_ms"}
    assert all(isinstance(value, (int, float)) and value >= 0 for value in stages.values())
    # The measured fetch is the fake's own 50 ms sleep — the number is real.
    assert stages["fetch_ms"] >= 40, stages
    # Nothing else can be blamed on a stage that never ran.
    assert stages["score_ms"] == 0.0 and stages["classify_ms"] == 0.0  # no AI slice without a profile
    assert report["elapsed_seconds"] >= stages["fetch_ms"] / 1000.0


@pytest.mark.asyncio
async def test_score_and_classify_stage_numbers_reflect_the_wave(db, owner, monkeypatch):
    """``score_ms`` / ``classify_ms`` are measured around their own calls."""
    _install_fetch_all(monkeypatch, [_posting(i) for i in range(6)])

    async def slow_score(profile, jd, **kwargs):
        await asyncio.sleep(0.05)
        return {"score": 88.0, "reason": "AI: strong", "score_source": "ai", "detail": {}}

    async def slow_size(company, jd, **kwargs):
        await asyncio.sleep(0.02)
        return ("medium", 0.8)

    monkeypatch.setattr(discovery_module, "score_job", slow_score)
    monkeypatch.setattr(discovery_module, "ai_company_size", slow_size)

    user = _user(db)
    report = await discovery_module.discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["lever"], profile={"skills": ["python"]},
    )

    stages = report["stages"]
    assert stages["pre_rank_ms"] >= 0
    assert stages["score_ms"] >= 40, stages
    assert stages["classify_ms"] >= 15, stages
    # Both slices overlap now: the classification wave is not an extra serial
    # stage, so its wall clock is inside the scoring wave's, not appended to it.
    assert stages["classify_ms"] <= stages["score_ms"] + 200, stages


# --------------------------------------------------------------------------- #
# 3. One AI wave + a per-run budget
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_classification_rides_the_scoring_wave(db, owner, monkeypatch):
    """The old serial stage is gone: classification starts *inside* the wave.

    The discriminator is the ``done_scores`` count at the moment the first
    classification call starts. Classification used to run only after the whole
    scoring wave had returned, so that count would already be four; in one wave
    it is zero — the scoring calls are still on the wire.
    """
    started, done = {"count": 0}, {"count": 0}
    classify_started, release = asyncio.Event(), asyncio.Event()

    async def score(profile, jd, **kwargs):
        started["count"] += 1
        try:
            await release.wait()
        finally:
            done["count"] += 1
        return {"score": 88.0, "reason": "AI: strong", "score_source": "ai", "detail": {}}

    async def classify(company, jd, **kwargs):
        assert started["count"] >= 1, "classification ran before any scoring call"
        assert done["count"] < 4, (
            f"classification was its own serial stage: {done['count']} of 4 scoring "
            "calls had already finished before the first one started"
        )
        classify_started.set()
        return ("medium", 0.8)

    monkeypatch.setattr(discovery_module, "score_job", score)
    monkeypatch.setattr(discovery_module, "ai_company_size", classify)

    # 4 candidates = exactly the default concurrency, so the whole scoring slice
    # is one wave and nothing is waiting on a semaphore slot.
    candidates = [_candidate(i) for i in range(4)]
    user = _user(db)
    task = asyncio.create_task(discovery_module._score_candidates(
        {"skills": ["python"]}, candidates, True, db=db, user_id=user.id, budget_seconds=5.0))
    await asyncio.wait_for(classify_started.wait(), 5.0)  # never set on the old, serial path
    release.set()
    stats = await asyncio.wait_for(task, 10.0)

    assert stats["scored"] == 4
    assert stats["classified"] == 4, "every candidate in the slice is inside AI_CLASSIFY_TOP here"
    assert stats["budget_exhausted"] is False
    for candidate in candidates:
        assert candidate["score"] == 88.0 and candidate["score_source"] == "ai"
    classified = [c for c in candidates if id(c) in stats["verdicts"]]
    assert len(classified) == 4
    assert all(c["company_size"] == "medium" for c in classified)


@pytest.mark.asyncio
async def test_the_budget_leaves_the_rest_preliminary_and_reports_the_cut(db, owner, monkeypatch):
    async def slow_score(profile, jd, **kwargs):
        await asyncio.sleep(0.5)
        return {"score": 88.0, "reason": "AI: strong", "score_source": "ai", "detail": {}}

    monkeypatch.setattr(discovery_module, "score_job", slow_score)

    candidates = [_candidate(i) for i in range(discovery_module.AI_RESCORE_TOP)]
    started = time.monotonic()
    stats = await asyncio.wait_for(discovery_module._score_candidates(
        {"skills": ["python"]}, candidates, True, db=None, user_id=None, budget_seconds=0.1), 10.0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.45, "the budget did not cut the wave short"
    assert stats["scored"] == 0 and stats["cut"] == len(candidates)
    assert stats["budget_exhausted"] is True
    assert all(c["score_source"] == "preliminary" for c in candidates)
    assert all("Preliminary keyword estimate" in c["score_reason"] for c in candidates)


@pytest.mark.asyncio
async def test_the_run_report_says_when_the_budget_cut_the_slice(db, owner, monkeypatch):
    """An operator must never have to infer it from the score distribution."""
    _install_fetch_all(monkeypatch, [_posting(i) for i in range(3)])

    async def cheap_slice(profile_data, candidates, use_ai, **kwargs):
        for candidate in candidates:
            candidate.setdefault("score", 50.0)
            candidate["score_source"] = "preliminary"
        return {"scored": 1, "classified": 0, "cut": 2, "budget_exhausted": True,
                "verdicts": set(), "pre_rank_ms": 0.0, "score_ms": 1.0, "classify_ms": 0.5}

    monkeypatch.setattr(discovery_module, "_score_candidates", cheap_slice)

    user = _user(db)
    report = await discovery_module.discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["lever"], profile={"skills": ["python"]},
    )

    assert report["ai_rescore"]["budget_exhausted"] is True
    assert report["ai_rescore"]["cut"] == 2
    assert report["ai_rescore"]["budget_seconds"] == discovery_module._ai_budget_seconds()
    assert report["stages"]["score_ms"] == 1.0 and report["stages"]["classify_ms"] == 0.5


@pytest.mark.asyncio
async def test_a_run_inside_its_budget_keeps_the_unchanged_ai_block(db, owner, monkeypatch):
    """No cut → the exact ``ai_rescore`` shape the UI has always read."""
    _install_fetch_all(monkeypatch, [_posting(0)])

    async def scored_slice(profile_data, candidates, use_ai, **kwargs):
        for candidate in candidates[:1]:
            candidate["score"] = 91.0
            candidate["score_source"] = "ai"
            candidate["score_reason"] = "AI: strong"
        return {"scored": 1, "classified": 1, "cut": 0, "budget_exhausted": False,
                "verdicts": {id(candidates[0])}, "pre_rank_ms": 0.1, "score_ms": 2.0,
                "classify_ms": 1.0}

    monkeypatch.setattr(discovery_module, "_score_candidates", scored_slice)
    user = _user(db)
    report = await discovery_module.discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["lever"], profile={"skills": ["python"]},
    )
    assert report["ai_rescore"] == {"enabled": True, "skipped": None, "scored": 1}


# --------------------------------------------------------------------------- #
# 4. A board source's fetch is non-destructive
# --------------------------------------------------------------------------- #
def _install_board_adapter(monkeypatch, *, slow_boards=(), board_budget: Optional[float] = None):
    """A real ``_BoardSource`` with deterministic boards (no network)."""
    from app.services.sources.adapters import ADAPTERS, _BoardSource

    class FakeBoards(_BoardSource):
        id, label = "greenhouse", "Greenhouse boards"

        async def boards(self, board_tokens):
            return list(self._tokens)

        async def fetch_board(self, token, limit):
            if token in slow_boards:
                await asyncio.sleep(30)
            return [Posting(title=f"Backend Engineer {token}", company=token.title(),
                            url=f"https://boards.example.com/{token}", source=self.id,
                            external_id=token, description="Python and PostgreSQL.")]

    adapter = FakeBoards()
    adapter._tokens = ["fast-one", "fast-two", *slow_boards]
    monkeypatch.setitem(ADAPTERS, "greenhouse", adapter)
    if board_budget is not None:
        monkeypatch.setattr(settings, "discovery_board_timeout_seconds", board_budget, raising=False)
    return adapter


@pytest.mark.asyncio
async def test_a_source_budget_keeps_the_boards_that_already_answered(monkeypatch):
    """It used to return ``(source, [], 'timeout')`` and throw the boards away."""
    from app.services import sources as registry

    _install_board_adapter(monkeypatch, slow_boards=("hanging",))
    before = _snapshot()
    postings, report = await registry.fetch_all(["python"], sources=["greenhouse"],
                                                timeout_seconds=1.5)

    assert "greenhouse" not in report["errors"], "a healthy-but-wide source still reported timeout"
    assert report["ok"]["greenhouse"] == 2, report
    assert {p.external_id for p in postings} == {"fast-one", "fast-two"}
    partial = report["partial"]["greenhouse"]
    assert partial["boards_total"] == 3 and partial["boards_answered"] == 2
    assert partial["boards_dropped"] == 1 and partial["boards_timed_out"] == 0
    assert partial["reason"] == "source_budget"
    assert partial["kept"] == 2
    assert _delta(before, 'jobhunter_source_partial_total{source="greenhouse"}') == 1


@pytest.mark.asyncio
async def test_one_hanging_board_cannot_spend_the_sources_whole_window(monkeypatch):
    """Each board has its own budget, so the source returns *normally*."""
    from app.services import sources as registry

    _install_board_adapter(monkeypatch, slow_boards=("hanging",), board_budget=0.2)
    started = time.monotonic()
    postings, report = await registry.fetch_all(["python"], sources=["greenhouse"],
                                                timeout_seconds=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"the hanging board held the source open for {elapsed:.1f}s"
    assert {p.external_id for p in postings} == {"fast-one", "fast-two"}
    partial = report["partial"]["greenhouse"]
    assert partial["boards_timed_out"] == 1 and partial["reason"] == "board_timeout"


# --------------------------------------------------------------------------- #
# 5. Search discovery: bounded-parallel queries and crawl
# --------------------------------------------------------------------------- #
@pytest.fixture
def search_user(db):
    row = User(email="latency-search@example.com", name="Searcher", password_hash="unused")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def search_env(monkeypatch):
    """The hermetic search environment the v2.3 suite uses, with a slow provider."""
    from app.services.search.providers import PROVIDERS, SearchResult

    monkeypatch.setattr(settings, "job_search_providers", "mock")
    monkeypatch.setattr(settings, "job_search_storage_rights", True)
    monkeypatch.setattr(settings, "job_search_queries_per_run", 3)
    calls: List[str] = []

    class SlowProvider:
        id = "mock"
        cost_microusd = 1000

        async def search(self, request):
            calls.append(request.query)
            await asyncio.sleep(0.2)
            slug = abs(hash(request.query)) % 1000
            return [SearchResult(f"https://careers.example.com/jobs/{slug}")]

    monkeypatch.setitem(PROVIDERS, "mock", SlowProvider)
    monkeypatch.setattr("app.services.search.fetch_page",
                        lambda url: _page(url))
    return calls


async def _page(url):
    import json as _json

    item = {"@type": "JobPosting", "title": "Backend Engineer", "hiringOrganization": {"name": "Acme"},
            "description": "<p>Python and PostgreSQL.</p>",
            "datePosted": datetime.utcnow().isoformat(),
            "validThrough": (datetime.utcnow() + timedelta(days=7)).isoformat()}
    return url, '<script type="application/ld+json">' + _json.dumps(item) + "</script>"


@pytest.mark.asyncio
async def test_search_queries_and_the_crawl_run_in_bounded_waves(db, search_user, search_env, monkeypatch):
    """Three queries × 0.2 s and their pages: serial it was ~1.2 s, waves ~0.4 s."""
    from app.services.search import discover_search
    from app.services.search.queries import SearchPreferences

    started = time.monotonic()
    jobs, report = await discover_search(db, search_user.id,
                                         SearchPreferences(("backend engineer",), True))
    elapsed = time.monotonic() - started

    assert len(search_env) == 3, f"every query must still reach the provider: {search_env}"
    assert report["attempts"] == 3
    assert elapsed < 0.75, f"queries were serialised ({elapsed:.2f}s)"
    assert jobs and report["validated"] >= 1
    assert report["pages_fetched"] >= 1


@pytest.mark.asyncio
async def test_the_crawl_keeps_its_page_budget_and_fifo_order(db, search_user, search_env, monkeypatch):
    from app.services.search import discover_search
    from app.services.search.queries import SearchPreferences

    monkeypatch.setattr(settings, "job_search_fetch_limit", 2)
    calls: List[str] = []

    async def page(url):
        calls.append(url)
        return await _page(url)

    monkeypatch.setattr("app.services.search.fetch_page", page)
    jobs, report = await discover_search(db, search_user.id,
                                         SearchPreferences(("backend engineer",), True))

    assert report["pages_fetched"] == 2, "a wave must not overshoot JOB_SEARCH_FETCH_LIMIT"
    assert len(calls) == 2 and jobs


# --------------------------------------------------------------------------- #
# 6. Per-pipeline slot budgets
# --------------------------------------------------------------------------- #
def test_the_shipped_default_caps_discovery_at_one_slot(db, owner, monkeypatch):
    monkeypatch.setattr(settings, "worker_pipeline_limits", "discovery=1", raising=False)
    assert pipeline_limits() == {"discovery": 1}
    # Unknown names and unparsable counts are ignored, never fatal.
    monkeypatch.setattr(settings, "worker_pipeline_limits", "nope=2,discovery=x", raising=False)
    assert pipeline_limits() == {}
    # 0 means "no budget" for that pipeline.
    monkeypatch.setattr(settings, "worker_pipeline_limits", "discovery=0", raising=False)
    assert pipeline_limits() == {}


def test_a_pipeline_budget_blocks_that_pipeline_without_starving_the_rest(db, owner):
    user = _user(db)
    first = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="budget:1")
    second = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="budget:2")
    email = enqueue(db, user_id=user.id, pipeline="email", dedupe_key="budget:email")

    limits = {"discovery": 1}
    first_claim = claim(db, pipelines=["discovery", "email"], limits=limits)
    assert first_claim is not None and first_claim.id == first.id
    # Discovery's one slot is busy: the second discovery waits, the email job —
    # behind it in the queue — does not. That is the whole point of the budget.
    claimed = claim(db, pipelines=["discovery", "email"], limits=limits)
    assert claimed is not None and claimed.id == email.id
    assert claim(db, pipelines=["discovery"], limits=limits) is None

    # With no budget at all the queue behaves exactly as it did before v2.4.
    unbudgeted = claim(db, pipelines=["discovery"], limits={})
    assert unbudgeted is not None and unbudgeted.id == second.id


def test_a_stalled_row_does_not_hold_a_pipeline_slot_forever(db, owner):
    """An expired lease means nobody is serving it — the reaper is coming."""
    user = _user(db)
    first = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="stalled:1")
    second = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="stalled:2")
    limits = {"discovery": 1}

    first_claim = claim(db, pipelines=["discovery"], limits=limits)
    assert first_claim is not None and first_claim.id == first.id
    assert claim(db, pipelines=["discovery"], limits=limits) is None
    # The worker died: the lease is in the past, so the slot is free again.
    row = db.get(PipelineJob, first.id)
    row.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
    db.commit()
    claimed = claim(db, pipelines=["discovery"], limits=limits)
    assert claimed is not None and claimed.id == second.id


# --------------------------------------------------------------------------- #
# 7. The tail after persistence
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_boards_events_are_one_commit(db, owner, monkeypatch):
    """One commit per created job's event was a second round trip per row."""
    calls: List[Dict[str, Any]] = []

    def spy(db_session, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(discovery_module, "record_job_event", spy)
    _install_fetch_all(monkeypatch, [_posting(i) for i in range(3)])
    user = _user(db)

    report = await discovery_module.discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["lever"],
    )

    discovered = [c for c in calls if c["stage"] == "discovered"]
    assert len(discovered) == report["inserted"] == 3
    assert all(c["commit"] is False for c in discovered), \
        "each event opened its own transaction instead of riding the batch"


@pytest.mark.asyncio
async def test_the_funding_flag_is_updated_from_this_runs_companies_only(db, owner, monkeypatch):
    """The targeted path: one IN query, and no full-board rescan."""
    user = _user(db)
    matched = FundingCompany(user_id=user.id, name="Northwind 0", name_normalized="northwind 0",
                             has_open_positions=False)
    stale_other = FundingCompany(user_id=user.id, name="Some Other Co", name_normalized="some other co",
                                 has_open_positions=True)
    db.add_all([matched, stale_other])
    db.commit()
    db.refresh(matched)
    db.refresh(stale_other)

    _install_fetch_all(monkeypatch, [_posting(0)])
    await discovery_module.discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["lever"],
    )

    db.expire_all()
    assert db.get(FundingCompany, matched.id).has_open_positions is True, \
        "the run's own created company was not linked to the radar"
    # A run that only *adds* jobs can never clear a flag, and must not touch
    # unrelated rows while pretending to recompute them.
    assert db.get(FundingCompany, stale_other.id).has_open_positions is True


@pytest.mark.asyncio
async def test_the_full_sweep_still_clears_a_flag_nobody_matches(db, owner, monkeypatch):
    """The other direction stays intact: the sync path recomputes both ways."""
    from app.services.funding_radar import refresh_funding_has_open_positions

    user = _user(db)
    orphan = FundingCompany(user_id=user.id, name="Nowhere Ltd", name_normalized="nowhere",
                            has_open_positions=True)
    db.add(orphan)
    db.commit()
    db.refresh(orphan)

    assert refresh_funding_has_open_positions(db, user.id) == 1
    db.expire_all()
    assert db.get(FundingCompany, orphan.id).has_open_positions is False
