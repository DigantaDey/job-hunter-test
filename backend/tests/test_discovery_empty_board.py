"""v2.2.4 — an empty board says *why* it is empty.

The gap this closes: a discovery run in a live environment without internet (or
with sources misconfigured) legitimately finds nothing, and the run's own report
has always carried the per-source status — but the product surfaced only "no
fresh jobs". A user could not tell *no sources enabled* from *every source
failed* from *nothing fresh in the window*, and the opt-in demo pool
(``INCLUDE_DEMO_POOL``, off by default everywhere) was another silent
contributor to empty boards that was never explained.

The contract under test:

* **Classification happens once, at run time**, in
  :func:`app.services.discovery.discover_for_user`, and only when the run added
  zero jobs to the board:

  - nothing was attempted (``report["sources"]["requested"]`` empty — no sources
    selected, or ``live_enabled`` off) → ``no_sources_configured``;
  - sources were attempted and **every** one is in ``errors`` →
    ``all_sources_failed``;
  - the fan-out kept something (at least one source in ``ok``) and nothing
    survived the window/dedupe → ``no_fresh_postings``.

* **A run that added jobs has no ``why_empty`` key at all** — not ``null``, not
  ``""``. An absent key is the claim "this run found jobs", so it is pinned as an
  absence. With the demo pool enabled a run always finds jobs, so ``why_empty``
  can never be set in that case — also pinned.

* **The ``summary`` block** is the accounting behind the verdict
  (``configured_sources`` / ``failed_sources`` / ``needs_credentials`` /
  ``demo_pool_enabled`` / ``freshness_window_hours`` / ``fetched`` / ``fresh``),
  derived from the same per-source report — and it rides on successful runs too.

* **``GET /api/jobs/discovery/last-run``** serves the newest *completed* run
  (``pipeline='discovery'``, ``status='done'``, a report in ``payload.result``):
  404 with the standard not-found shape when the user has none, tenant-scoped,
  with an optional ``?persona_id=`` filter applied **in Python** (SQLite JSON
  subscripting is unreliable), and only the summary blocks on the wire — never
  the full report, which embeds the run's job rows.

* **The related bug fixed in the same PR**: an explicitly-empty source list used
  to be silently upgraded to "all built-in sources" twice over
  (``handle_discovery``'s ``payload.get("sources") or config["sources"]`` and
  ``fetch_all``'s ``sources or available_source_ids()``), so a user who
  deliberately selected zero sources was fetched from every available adapter
  and ``no_sources_configured`` was unreachable. ``None`` now means "use the
  defaults" and ``[]`` means "fetch nothing", end to end.

Hermetic: the source fan-out and the adapters behind it are faked, the
deterministic AI stand-in from ``conftest`` answers anything AI-backed, and no
test touches the network.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import event

from app.services.sources.base import Posting, SourceError

#: The three reasons, spelled out so a typo in the product cannot be mirrored by
#: a typo in the tests.
WHY_NO_SOURCES = "no_sources_configured"
WHY_ALL_FAILED = "all_sources_failed"
WHY_NOTHING_FRESH = "no_fresh_postings"

LAST_RUN = "/api/jobs/discovery/last-run"

#: A description that overlaps the stand-in profile, so the deterministic
#: pre-rank has something to work with (the AI slice is never reached in these
#: tests: no profile is passed to ``discover_for_user``).
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


def _posting(index: int, *, hours_ago: float = 1.0, source: str = "lever") -> Posting:
    return Posting(
        title=f"Backend Engineer {index}",
        company=f"Paystack {index}",
        url=f"https://jobs.example.com/posting-{index}",
        source=source,
        external_id=f"ext-{source}-{index}",
        location="Remote",
        description=JD,
        posted_at=datetime.utcnow() - timedelta(hours=hours_ago),
    )


def _source_report(*, requested: List[str], ok: Optional[Dict[str, int]] = None,
                   errors: Optional[Dict[str, str]] = None,
                   total: Optional[int] = None) -> Dict[str, Any]:
    """The exact per-source report shape :func:`app.services.sources.fetch_all` returns."""
    kept = dict(ok or {})
    failed = dict(errors or {})
    return {"requested": list(requested), "ok": kept, "errors": failed, "skipped": {},
            "partial": {}, "total": sum(kept.values()) if total is None else total}


def _install_fetch_all(monkeypatch, *, postings: Optional[List[Posting]] = None,
                       requested: Optional[List[str]] = None,
                       ok: Optional[Dict[str, int]] = None,
                       errors: Optional[Dict[str, str]] = None,
                       total: Optional[int] = None,
                       calls: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Replace the registry's fan-out with a fixed report.

    ``discover_for_user`` calls ``sources.fetch_all`` through the module object,
    so patching the attribute is enough — the same double the v2.2.3 suite uses.
    ``calls`` records what the pipeline handed the registry, which is how the
    unset-vs-empty source list is pinned without reimplementing ``fetch_all``.
    """
    from app.services import sources as registry

    report = _source_report(requested=requested or [], ok=ok, errors=errors, total=total)
    returned = list(postings or [])

    async def fake_fetch_all(keywords, *, limit=40, since_hours=168, sources=None,
                             board_tokens=None, timeout_seconds=45.0):
        if calls is not None:
            calls.append({"keywords": list(keywords), "sources": sources, "limit": limit,
                          "since_hours": since_hours, "board_tokens": board_tokens})
        return list(returned), copy.deepcopy(report)

    monkeypatch.setattr(registry, "fetch_all", fake_fetch_all)
    return report


def _install_adapters(monkeypatch, *, failing: tuple = (), stale: bool = False,
                      calls: Optional[List[str]] = None) -> None:
    """Keep the REAL :func:`fetch_all` and fake only the per-source fetch.

    This is the honest way to test the report's ``ok``/``errors`` bookkeeping:
    the fan-out, its cutoff filter and its dedupe all stay real.
    """
    from app.services import sources as registry

    async def fake_fetch_from_source(source_id, keywords, **kwargs):
        if calls is not None:
            calls.append(source_id)
        if source_id in failing:
            raise SourceError(f"{source_id} upstream down")
        hours_ago = 24 * 30 if stale else 1.0
        return [_posting(index, hours_ago=hours_ago, source=source_id) for index in range(2)]

    monkeypatch.setattr(registry, "fetch_from_source", fake_fetch_from_source)


async def _discover(db, user, **kwargs) -> Dict[str, Any]:
    """One discovery pass with no profile → deterministic pre-rank, zero AI calls."""
    from app.services.discovery import discover_for_user

    params: Dict[str, Any] = {"keywords": ["python backend"], "freshness_hours": 48, "limit": 30,
                              "live_enabled": True, "source_ids": ["lever"]}
    params.update(kwargs)
    return await discover_for_user(db, user, **params)


def _job_entry(index: int, *, score: float = 80.0) -> Dict[str, Any]:
    return {"id": index, "title": f"Backend Engineer {index}", "company": f"Paystack {index}",
            "source": "lever", "score": score, "location": "Remote",
            "url": f"https://jobs.example.com/posting-{index}"}


def _queue_run(db, user_id: int, dedupe_key: str, *, jobs: Optional[List[Dict[str, Any]]] = None,
               why_empty: Optional[str] = None, summary: Optional[Dict[str, Any]] = None,
               ai_rescore: Optional[Dict[str, Any]] = None, persona_id: Optional[int] = None,
               status: str = "done", with_result: bool = True,
               started_at: Optional[datetime] = None,
               sources: Optional[Dict[str, Any]] = None, pipeline: str = "discovery"):
    """A queue row shaped exactly like one the worker completed."""
    from app.models.models import PipelineJob

    found = list(jobs or [])
    report: Dict[str, Any] = {
        "started_at": (started_at or datetime.utcnow()).isoformat(),
        "keywords": ["python backend"],
        "sources": sources if sources is not None else _source_report(requested=["lever"],
                                                                     ok={"lever": len(found)}),
        "scanned": len(found),
        "fresh": len(found),
        "inserted": len(found),
        "elapsed_seconds": 0.4,
        "ai_rescore": ai_rescore if ai_rescore is not None else {
            "enabled": False, "skipped": "no_profile", "scored": 0},
        "jobs": found,
    }
    if summary is not None:
        report["summary"] = summary
    if why_empty is not None:
        report["why_empty"] = why_empty
    payload: Dict[str, Any] = {"keywords": ["python backend"], "freshness_hours": 48, "limit": 30,
                               "live_enabled": True, "sources": ["lever"], "persona_id": persona_id}
    if with_result:
        payload["result"] = report
    item = PipelineJob(user_id=user_id, pipeline=pipeline, status=status, payload=payload,
                       dedupe_key=dedupe_key, finished_at=datetime.utcnow())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


async def _run_item(item_id: int) -> None:
    """Execute one queued item exactly like a worker slot does."""
    from app.worker import Worker

    await Worker(pipelines=["discovery"])._run_item(item_id, "discovery")


def _enqueue(db, user_id: int, dedupe_key: str, **payload: Any):
    from app.services.job_queue import enqueue

    body = {"keywords": ["python backend"], "freshness_hours": 48, "limit": 30, "live_enabled": True}
    body.update(payload)
    item = enqueue(db, user_id=user_id, pipeline="discovery", dedupe_key=dedupe_key, payload=body)
    assert item is not None
    _sync(db)
    return item


def _report_of(db, item) -> Dict[str, Any]:
    """The discovery report as it rides in the queue item's result JSON."""
    _sync(db)
    db.refresh(item)
    payload = dict(item.payload or {})
    assert "result" in payload, payload
    return dict(payload["result"])


# --------------------------------------------------------------------------- #
# 1. Classification: exactly one reason, and only when the run added nothing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_nothing_attempted_is_no_sources_configured(client, auth, db, monkeypatch):
    """An empty ``requested`` list means nothing was fetched — a configuration
    problem, not a quiet market."""
    from app.services.discovery import WHY_NO_SOURCES_CONFIGURED

    _install_fetch_all(monkeypatch, postings=[], requested=[])
    report = await _discover(db, _user(db), source_ids=[])

    assert report["why_empty"] == WHY_NO_SOURCES == WHY_NO_SOURCES_CONFIGURED
    assert report["inserted"] == 0 and report["jobs"] == []
    assert report["summary"]["configured_sources"] == []
    assert report["summary"]["failed_sources"] == {}, "nothing failed — nothing ran"
    assert report["summary"]["fetched"] == 0 and report["summary"]["fresh"] == 0


@pytest.mark.asyncio
async def test_live_scraping_off_attempted_nothing(client, auth, db, monkeypatch):
    """``live_enabled=False`` skips the fan-out entirely, so the source report is
    empty and the honest reason is still "no sources configured" — the user
    switched discovery off, and the banner can say so."""
    calls: List[Dict[str, Any]] = []
    _install_fetch_all(monkeypatch, postings=[], requested=["lever"], ok={"lever": 2}, calls=calls)

    report = await _discover(db, _user(db), live_enabled=False)

    assert calls == [], "the fan-out must not run when live discovery is off"
    assert report["why_empty"] == WHY_NO_SOURCES
    assert report["summary"]["configured_sources"] == []
    assert report["summary"]["demo_pool_enabled"] is False, "the demo pool is opt-in everywhere"


@pytest.mark.asyncio
async def test_every_requested_source_failing_is_all_sources_failed(client, auth, db, monkeypatch):
    """Real fan-out, every adapter raising: individual failures never propagate —
    they land in ``errors``, and the run reports an outage, not a quiet market."""
    calls: List[str] = []
    _install_adapters(monkeypatch, failing=("lever", "greenhouse"), calls=calls)

    report = await _discover(db, _user(db), source_ids=["lever", "greenhouse"])

    assert sorted(calls) == ["greenhouse", "lever"]
    assert report["why_empty"] == WHY_ALL_FAILED
    assert report["summary"]["failed_sources"] == {"lever": "lever upstream down",
                                                   "greenhouse": "greenhouse upstream down"}
    assert report["summary"]["configured_sources"] == ["lever", "greenhouse"]
    assert report["summary"]["fetched"] == 0 and report["summary"]["fresh"] == 0
    assert report["sources"]["ok"] == {}, "a failed source is never also a kept-count"


@pytest.mark.asyncio
async def test_healthy_sources_with_nothing_in_the_window_are_no_fresh_postings(
        client, auth, db, monkeypatch):
    """Real fan-out, adapters answering with postings older than the window: the
    sources are healthy (``ok``, not ``errors``) and there is simply nothing
    fresh. ``fetch_all``'s own cutoff drops them, so ``kept`` is 0."""
    calls: List[str] = []
    _install_adapters(monkeypatch, stale=True, calls=calls)

    report = await _discover(db, _user(db), source_ids=["remotive"], freshness_hours=24)

    assert calls == ["remotive"]
    assert report["why_empty"] == WHY_NOTHING_FRESH
    assert report["sources"]["ok"] == {"remotive": 0}, report["sources"]
    assert report["sources"]["errors"] == {}
    assert report["summary"]["configured_sources"] == ["remotive"]
    assert report["summary"]["failed_sources"] == {}
    assert report["summary"]["fetched"] == 0 and report["summary"]["fresh"] == 0
    assert report["summary"]["freshness_window_hours"] == 24


@pytest.mark.asyncio
async def test_a_run_that_added_jobs_carries_no_why_empty_key(client, auth, db, monkeypatch):
    """The field is *absent* — not ``null``, not ``""`` — so an absent key means
    "this run found jobs" and a reader never has to guess about a falsy value."""
    from app.services.discovery import WHY_ALL_SOURCES_FAILED, WHY_NO_FRESH_POSTINGS, WHY_NO_SOURCES_CONFIGURED

    _install_fetch_all(monkeypatch, postings=[_posting(0), _posting(1)], requested=["lever"],
                       ok={"lever": 2})
    report = await _discover(db, _user(db))

    assert report["inserted"] == 2 and len(report["jobs"]) == 2
    assert "why_empty" not in report, report.get("why_empty")
    assert report.get("why_empty", "absent") == "absent"
    assert "why_empty" not in json.dumps(report), "and it is absent from the persisted JSON too"
    # The vocabulary is a closed set of three (no fourth "all duplicates" reason:
    # a run whose candidates were all already on the board did not empty it).
    assert {WHY_NO_SOURCES_CONFIGURED, WHY_ALL_SOURCES_FAILED,
            WHY_NO_FRESH_POSTINGS} == {WHY_NO_SOURCES, WHY_ALL_FAILED, WHY_NOTHING_FRESH}


@pytest.mark.asyncio
async def test_the_demo_pool_never_produces_a_why_empty(client, auth, db, monkeypatch):
    """With ``INCLUDE_DEMO_POOL`` on a run always finds jobs, so ``why_empty`` can
    never be set — and the report says the demo pool contributed, which is the
    other half of an honest empty-board story."""
    _install_fetch_all(monkeypatch, postings=[], requested=[])
    monkeypatch.setattr("app.core.config.settings.include_demo_pool", True)

    report = await _discover(db, _user(db), source_ids=[])

    assert report["demo_pool"] is True
    assert report["inserted"] > 0, "the demo pool always contributes when it is enabled"
    assert "why_empty" not in report, report.get("why_empty")
    assert report["summary"]["demo_pool_enabled"] is True


# --------------------------------------------------------------------------- #
# 2. The summary block is the source report, recounted — never a second truth
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_summary_block_matches_the_source_report(client, auth, db, monkeypatch):
    """One source kept three postings, one timed out: the summary must say exactly
    that, with the run's own window and the registry's credential gaps."""
    from app.services import sources as registry

    monkeypatch.setattr(registry, "unconfigured_source_ids", lambda: ["adzuna", "jooble"])
    _install_fetch_all(monkeypatch, postings=[_posting(i) for i in range(3)],
                       requested=["lever", "greenhouse"], ok={"lever": 3},
                       errors={"greenhouse": "timeout"})

    report = await _discover(db, _user(db), source_ids=["lever", "greenhouse"], freshness_hours=72)

    assert report["summary"] == {
        "configured_sources": ["lever", "greenhouse"],
        "failed_sources": {"greenhouse": "timeout"},
        # v2.4: sources that answered with only some of their boards. Empty here
        # (this run's fan-out is a fixed report), present on every run so a
        # reader never has to special-case "the key is missing".
        "partial_sources": {},
        "needs_credentials": ["adzuna", "jooble"],
        "demo_pool_enabled": False,
        "freshness_window_hours": 72,
        "fetched": 3,
        "fresh": 3,
    }
    # Derived from the same report the classification reads, not a second count.
    assert report["summary"]["configured_sources"] == report["sources"]["requested"]
    assert report["summary"]["failed_sources"] == report["sources"]["errors"]
    assert report["summary"]["fetched"] == report["sources"]["total"]
    assert report["summary"]["fresh"] == report["fresh"]


@pytest.mark.asyncio
async def test_summary_rides_on_successful_runs_too(client, auth, db, monkeypatch):
    """Pinned decision: ``summary`` is the run's accounting, so it is present on
    every report — only ``why_empty`` is conditional."""
    _install_fetch_all(monkeypatch, postings=[_posting(0)], requested=["lever"], ok={"lever": 1})

    report = await _discover(db, _user(db))

    assert report["inserted"] == 1 and "why_empty" not in report
    assert report["summary"]["fetched"] == 1 and report["summary"]["fresh"] == 1
    assert report["summary"]["configured_sources"] == ["lever"]


def test_needs_credentials_is_the_registry_helper_not_a_constant(monkeypatch):
    """``needs_credentials`` is :func:`unconfigured_source_ids` verbatim.

    Worth pinning on its own: with no partner credentials set, every key-gated
    adapter (Adzuna, Jooble, USAJOBS) reports ``available() == False`` *and*
    ``configured() == available()``, so the helper returns ``[]`` in a default
    deployment. The key is wired to the helper either way, so it starts telling
    the truth the moment an adapter separates "available" from "configured".
    """
    from app.services import sources as registry
    from app.services.discovery import _run_summary

    assert registry.unconfigured_source_ids() == [], \
        "with today's adapters the helper is always empty: the key-gated ones report configured()==available()"
    monkeypatch.setattr(registry, "unconfigured_source_ids", lambda: ["usajobs"])
    summary = _run_summary(_source_report(requested=["lever"], ok={"lever": 0}),
                           freshness_hours=48, fresh_count=0)
    assert summary["needs_credentials"] == ["usajobs"]


# --------------------------------------------------------------------------- #
# 3. GET /api/jobs/discovery/last-run
# --------------------------------------------------------------------------- #
def test_last_run_404s_when_the_user_has_no_completed_run(client, auth, db):
    """No runs at all → the standard not-found shape, which the banner renders as
    "No discovery run yet" (and never as a reason)."""
    response = client.get(LAST_RUN, headers=auth)
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"detail"}, body
    assert "discovery run" in str(body["detail"]).lower()


def test_last_run_is_authenticated_like_the_rest_of_the_jobs_api(client, auth):
    """No token → 401; a read-scoped API key → the same answer as the owner.

    The 404 (rather than a routing 404) is also the reachability pin: the static
    path is not swallowed by ``/api/jobs/{job_id}``.
    """
    from app.main import app

    assert client.get(LAST_RUN).status_code == 401
    assert client.get(LAST_RUN, headers={"Authorization": "Bearer not-a-token"}).status_code == 401

    raw = client.post("/api/auth/api-keys", json={"name": "reader", "scopes": ["read"]},
                      headers=auth).json()["api_key"]
    readable = client.get(LAST_RUN, headers={"X-API-Key": raw})
    assert readable.status_code == 404, readable.text
    assert "discovery run" in str(readable.json()["detail"]).lower()

    # The contract the SPA codes against is in the schema it is generated from.
    schema = app.openapi()["paths"][LAST_RUN]["get"]
    params = {p["name"]: p for p in schema.get("parameters", [])}
    assert "persona_id" in params and params["persona_id"]["in"] == "query"
    assert params["persona_id"]["required"] is False


def test_last_run_ignores_runs_that_are_not_completed(client, auth, db):
    """A queued, processing, paused, failed or dead run has no report and must not
    produce a banner state — including when it is the newest row."""
    user = _user(db)
    for index, status in enumerate(("queued", "processing", "paused", "failed", "dead")):
        _queue_run(db, user.id, f"not-completed-{index}", status=status, with_result=False)

    response = client.get(LAST_RUN, headers=auth)
    assert response.status_code == 404, response.text


def test_last_run_returns_the_newest_completed_run(client, auth, db):
    """Two completed runs: the newer one wins, and only its summary blocks travel."""
    user = _user(db)
    summary = {"configured_sources": ["lever"], "failed_sources": {}, "needs_credentials": [],
               "demo_pool_enabled": False, "freshness_window_hours": 48, "fetched": 2, "fresh": 2}
    _queue_run(db, user.id, "older", started_at=datetime.utcnow() - timedelta(days=2),
               why_empty=WHY_ALL_FAILED, summary=summary)
    newer = _queue_run(db, user.id, "newer", started_at=datetime.utcnow() - timedelta(hours=1),
                       jobs=[_job_entry(1), _job_entry(2)], summary=summary,
                       ai_rescore={"enabled": True, "skipped": None, "scored": 2})

    response = client.get(LAST_RUN, headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["at"] == (newer.payload["result"])["started_at"]
    assert body["added"] == 2
    assert body["summary"] == summary
    assert body["ai_rescore"] == {"enabled": True, "skipped": None, "scored": 2}, "pass-through"
    assert "why_empty" not in body, "a run that found jobs has no reason to be empty"


def test_last_run_never_returns_the_full_report(client, auth, db):
    """The stored report embeds the run's job rows (and grows with every run); the
    endpoint returns the summary blocks only."""
    user = _user(db)
    _queue_run(db, user.id, "heavy", jobs=[_job_entry(i) for i in range(3)],
               why_empty=None,
               summary={"configured_sources": ["lever"], "failed_sources": {},
                        "needs_credentials": [], "demo_pool_enabled": False,
                        "freshness_window_hours": 48, "fetched": 3, "fresh": 3})

    body = client.get(LAST_RUN, headers=auth).json()
    assert set(body) <= {"at", "added", "why_empty", "summary", "ai_rescore"}, set(body)
    for leaked in ("jobs", "sources", "keywords", "scanned", "elapsed_seconds"):
        assert leaked not in body, f"the full report must not be served: {leaked}"


@pytest.mark.parametrize("reason", [WHY_NO_SOURCES, WHY_ALL_FAILED, WHY_NOTHING_FRESH])
def test_last_run_passes_why_empty_through_verbatim(client, auth, db, reason):
    """The endpoint reads the stored classification; it never re-derives one."""
    user = _user(db)
    summary = {"configured_sources": [] if reason == WHY_NO_SOURCES else ["lever", "greenhouse"],
               "failed_sources": ({"lever": "upstream down", "greenhouse": "timeout"}
                                  if reason == WHY_ALL_FAILED else {}),
               "needs_credentials": [], "demo_pool_enabled": False,
               "freshness_window_hours": 48, "fetched": 0, "fresh": 0}
    _queue_run(db, user.id, f"why-{reason}", why_empty=reason, summary=summary)

    body = client.get(LAST_RUN, headers=auth)
    assert body.status_code == 200, body.text
    assert body.json()["why_empty"] == reason
    assert body.json()["added"] == 0
    assert body.json()["summary"] == summary


def test_a_dead_run_does_not_shadow_the_completed_one(client, auth, db):
    """A newer dead row (an AI needs-action failure) has no report; the scan skips
    it instead of stopping, so the last *completed* run still answers."""
    user = _user(db)
    completed = _queue_run(db, user.id, "completed", started_at=datetime.utcnow() - timedelta(hours=3),
                           why_empty=WHY_NOTHING_FRESH,
                           summary={"configured_sources": ["lever"], "failed_sources": {},
                                    "needs_credentials": [], "demo_pool_enabled": False,
                                    "freshness_window_hours": 48, "fetched": 0, "fresh": 0})
    _queue_run(db, user.id, "dead-later", status="dead", with_result=False)
    # …and a *completed* row that somehow carries no report is skipped the same way.
    _queue_run(db, user.id, "done-without-report", status="done", with_result=False)

    body = client.get(LAST_RUN, headers=auth).json()
    assert body["at"] == completed.payload["result"]["started_at"]
    assert body["why_empty"] == WHY_NOTHING_FRESH


def test_last_run_is_tenant_scoped(client, auth, member, member_auth, db):
    """User B never sees user A's runs — B gets the same 404 as a user with none."""
    owner = _user(db)
    _queue_run(db, owner.id, "owner-run", why_empty=WHY_ALL_FAILED,
               summary={"configured_sources": ["lever"], "failed_sources": {"lever": "down"},
                        "needs_credentials": [], "demo_pool_enabled": False,
                        "freshness_window_hours": 48, "fetched": 0, "fresh": 0})

    assert client.get(LAST_RUN, headers=auth).status_code == 200
    denied = client.get(LAST_RUN, headers=member_auth)
    assert denied.status_code == 404, denied.text
    assert set(denied.json()) == {"detail"}


def test_persona_filter_matches_the_queue_payload(client, auth, db):
    """``?persona_id=`` selects the newest run *for that track*, which need not be
    the newest run overall."""
    user = _user(db)
    _queue_run(db, user.id, "track-2", persona_id=2, started_at=datetime.utcnow() - timedelta(days=1),
               why_empty=WHY_NO_SOURCES,
               summary={"configured_sources": [], "failed_sources": {}, "needs_credentials": [],
                        "demo_pool_enabled": False, "freshness_window_hours": 48,
                        "fetched": 0, "fresh": 0})
    newest = _queue_run(db, user.id, "track-1", persona_id=1,
                        jobs=[_job_entry(7)],
                        summary={"configured_sources": ["lever"], "failed_sources": {},
                                 "needs_credentials": [], "demo_pool_enabled": False,
                                 "freshness_window_hours": 48, "fetched": 1, "fresh": 1})

    unfiltered = client.get(LAST_RUN, headers=auth).json()
    assert unfiltered["at"] == newest.payload["result"]["started_at"], "no filter → the newest run"

    track_one = client.get(LAST_RUN, params={"persona_id": 1}, headers=auth).json()
    assert track_one["at"] == newest.payload["result"]["started_at"] and track_one["added"] == 1

    track_two = client.get(LAST_RUN, params={"persona_id": 2}, headers=auth)
    assert track_two.status_code == 200, track_two.text
    assert track_two.json()["why_empty"] == WHY_NO_SOURCES, "the older run for *this* track"

    assert client.get(LAST_RUN, params={"persona_id": 99}, headers=auth).status_code == 404


def test_persona_filter_normalises_a_string_id_in_the_payload(client, auth, db):
    """A payload that carries ``"persona_id": "2"`` still answers ``?persona_id=2``.

    This is the case a SQL JSON subscript gets wrong on one backend or the other
    (SQLite compares an int, PostgreSQL hands back text), and an unscoped run
    (``persona_id`` absent or ``None``) never matches a filter at all.
    """
    user = _user(db)
    as_string = _queue_run(db, user.id, "track-as-string", persona_id="4",
                           why_empty=WHY_ALL_FAILED,
                           summary={"configured_sources": ["lever"],
                                    "failed_sources": {"lever": "upstream down"},
                                    "needs_credentials": [], "demo_pool_enabled": False,
                                    "freshness_window_hours": 48, "fetched": 0, "fresh": 0})
    unscoped = _queue_run(db, user.id, "unscoped", persona_id=None, jobs=[_job_entry(1)])

    matched = client.get(LAST_RUN, params={"persona_id": 4}, headers=auth)
    assert matched.status_code == 200, matched.text
    assert matched.json()["at"] == as_string.payload["result"]["started_at"]

    # The newest run is unscoped, so it is what an unfiltered read returns — and a
    # filter for a track it was not run for finds nothing.
    assert client.get(LAST_RUN, headers=auth).json()["added"] == 1
    assert unscoped.payload["persona_id"] is None
    assert client.get(LAST_RUN, params={"persona_id": 5}, headers=auth).status_code == 404


def test_persona_filter_is_python_side_not_a_json_subscript(client, auth, db):
    """The persona filter must not reach SQL.

    ``payload['persona_id'].as_integer()`` compiles to ``json_extract`` on SQLite
    and to a *text* comparison on PostgreSQL, so the same filter matches on one
    backend and silently matches nothing on the other. This pins the query the
    endpoint actually emits: no JSON subscripting, and no ``persona_id`` in the
    WHERE clause at all.
    """
    from app.db import engine

    user = _user(db)
    _queue_run(db, user.id, "sql-check", persona_id=3, jobs=[_job_entry(1)])

    statements: List[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        response = client.get(LAST_RUN, params={"persona_id": 3}, headers=auth)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert response.status_code == 200, response.text
    queue_queries = [s for s in statements if "pipeline_jobs" in s]
    assert queue_queries, "the endpoint never queried the queue"
    for statement in queue_queries:
        lowered = statement.lower()
        assert "persona_id" not in lowered, statement
        assert "json_extract" not in lowered, statement
        assert "->>" not in statement and "->" not in statement, statement


@pytest.mark.asyncio
async def test_last_run_serves_what_the_worker_persisted(client, auth, db, monkeypatch):
    """End to end: trigger → worker → ``payload.result`` → endpoint.

    The classification is written once, at run time, and read back verbatim — the
    endpoint derives nothing.
    """
    calls: List[Dict[str, Any]] = []
    _install_fetch_all(monkeypatch, postings=[], requested=["remotive"], ok={"remotive": 0}, calls=calls)

    triggered = client.post("/api/jobs/discover",
                            # ``live_enabled`` is explicit: the suite runs with
                            # LIVE_SCRAPING_ENABLED=false, and a run that skips the
                            # fan-out attempted nothing — which is the *other*
                            # reason (pinned above), not "nothing fresh".
                            json={"keywords": ["python backend"], "freshness_hours": 48,
                                  "sources": ["remotive"], "live_enabled": True},
                            headers=auth)
    assert triggered.status_code == 200, triggered.text
    item_id = triggered.json()["pipeline_job_id"]
    assert item_id, triggered.json()

    await _run_item(item_id)

    body = client.get(LAST_RUN, headers=auth)
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["added"] == 0
    assert payload["why_empty"] == WHY_NOTHING_FRESH
    assert payload["summary"]["configured_sources"] == ["remotive"]
    assert payload["summary"]["freshness_window_hours"] == 48
    assert payload["ai_rescore"]["enabled"] is False, "no profile on file → no AI slice"
    assert calls and calls[0]["sources"] == ["remotive"]


# --------------------------------------------------------------------------- #
# 4. The related bug: an explicitly-empty source list is not "all sources"
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_all_honours_an_explicitly_empty_source_list(monkeypatch):
    """``None`` → every available adapter; ``[]`` → nothing fetched at all."""
    from app.services.sources import available_source_ids, fetch_all

    calls: List[str] = []
    _install_adapters(monkeypatch, calls=calls)

    postings, report = await fetch_all(["python"], limit=10, sources=[])
    assert postings == []
    assert report["requested"] == [] and report["total"] == 0
    assert report["ok"] == {} and report["errors"] == {}
    assert calls == [], "an explicit empty list must not fetch a single adapter"

    calls.clear()
    _postings, defaults = await fetch_all(["python"], limit=10, sources=None)
    assert defaults["requested"] == available_source_ids(), "unset still means every available adapter"
    assert calls, "and it really does fetch them"


@pytest.mark.asyncio
async def test_a_saved_empty_source_list_fetches_nothing_end_to_end(client, auth, db, monkeypatch):
    """The whole chain respects ``[]``: settings → config → handler → fan-out.

    On main this fetched every available adapter (``payload.get("sources") or
    config["sources"]`` and then ``sources or available_source_ids()``), so a user
    who had deliberately saved zero sources still got a full fan-out — and an
    empty board could never be reported as ``no_sources_configured``.
    """
    from app.services.discovery import discovery_sources_config
    from app.services.user_settings import set_setting

    calls: List[str] = []
    _install_adapters(monkeypatch, calls=calls)
    user = _user(db)
    set_setting(db, int(user.id), "scraping", "sources", [])
    db.commit()
    _sync(db)

    config = discovery_sources_config(db, int(user.id))
    assert config["sources"] == [], "the config layer already distinguished None from []"

    item = _enqueue(db, int(user.id), "saved-empty-sources")  # no "sources" key → the config's
    await _run_item(item.id)

    _sync(db)
    db.refresh(item)
    assert item.status == "done", f"{item.status}: {item.error}"
    report = _report_of(db, item)
    assert calls == [], "not one adapter may be fetched"
    assert report["sources"]["requested"] == []
    assert report["why_empty"] == WHY_NO_SOURCES
    assert report["summary"]["configured_sources"] == []
    assert report["inserted"] == 0


@pytest.mark.asyncio
async def test_handle_discovery_keeps_unset_and_empty_apart(client, auth, db, monkeypatch):
    """What the handler hands the fan-out: the config when the payload is silent,
    the payload's own ``[]`` when it is not."""
    from app.services.user_settings import set_setting

    user = _user(db)
    set_setting(db, int(user.id), "scraping", "sources", ["lever", "remotive"])
    db.commit()
    _sync(db)

    calls: List[Dict[str, Any]] = []
    _install_fetch_all(monkeypatch, postings=[], requested=[], calls=calls)

    explicit = _enqueue(db, int(user.id), "explicit-empty", sources=[])
    await _run_item(explicit.id)
    assert calls[-1]["sources"] == [], "an explicit empty list must survive to the fan-out"

    unset = _enqueue(db, int(user.id), "unset-sources", freshness_hours=72)
    await _run_item(unset.id)
    assert calls[-1]["sources"] == ["lever", "remotive"], "absent means the user's configuration"


def test_payload_sources_resolution_is_total():
    """``_payload_sources`` is the one place the queue payload's ``sources`` key is
    interpreted: absent/None → config, list (including empty) → verbatim,
    anything else → config rather than "everything"."""
    from app.services.handlers import _payload_sources

    config = {"sources": ["lever", "remotive"]}
    assert _payload_sources({}, config) == ["lever", "remotive"]
    assert _payload_sources({"sources": None}, config) == ["lever", "remotive"]
    assert _payload_sources({"sources": []}, config) == []
    assert _payload_sources({"sources": ["greenhouse"]}, config) == ["greenhouse"]
    assert _payload_sources({"sources": ("greenhouse", "ashby")}, config) == ["greenhouse", "ashby"]
    # The settings layer accepts a comma-separated string for the same key.
    assert _payload_sources({"sources": "lever, remotive"}, config) == ["lever", "remotive"]
    # A value nobody can interpret falls back to the configuration — never to
    # "every available adapter", which is what a bad row must not widen a run to.
    assert _payload_sources({"sources": 42}, config) == ["lever", "remotive"]
    assert _payload_sources(None, config) == ["lever", "remotive"]
    # …and an empty *configuration* is passed through as empty, not defaulted.
    assert _payload_sources({}, {"sources": []}) == []
    assert _payload_sources({"sources": []}, {"sources": []}) == []


def test_discover_endpoint_queues_an_explicit_empty_source_list(client, auth, db):
    """``POST /api/jobs/discover`` with ``sources: []`` queues (and reports) an
    empty selection instead of quietly widening the run."""
    from app.models.models import PipelineJob

    empty = client.post("/api/jobs/discover", json={"keywords": ["python"], "freshness_hours": 24,
                                                    "sources": []}, headers=auth)
    assert empty.status_code == 200, empty.text
    assert empty.json()["sources"] == [], "the response must not claim sources the caller refused"
    _sync(db)
    queued = db.query(PipelineJob).filter(PipelineJob.id == empty.json()["pipeline_job_id"]).one()
    assert queued.payload["sources"] == []

    # Unset still means "the user's configuration" — the defaults here.
    default = client.post("/api/jobs/discover", json={"keywords": ["python"], "freshness_hours": 96},
                          headers=auth)
    assert default.status_code == 200, default.text
    assert default.json()["sources"], default.json()
    _sync(db)
    queued_default = db.query(PipelineJob).filter(PipelineJob.id == default.json()["pipeline_job_id"]).one()
    assert queued_default.payload["sources"] == default.json()["sources"]


def test_settings_ui_cannot_save_an_empty_source_list_yet():
    """Documents the reachability of ``no_sources_configured`` from the UI.

    The Settings page renders ``scraping.sources`` as read-only chips, so today an
    explicit ``[]`` can only be written through ``PUT /api/settings`` (or a
    ``sources: []`` discovery request). The switch a user *can* flip is
    ``live_enabled``, which skips the fan-out entirely and lands in the same
    reason — so the banner state is reachable from the product's own UI either
    way. Pinned as a static check so the day a source picker lands, this note is
    revisited rather than silently going stale.
    """
    import os
    import re

    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                        "frontend", "src", "pages", "Settings.tsx"))
    source = open(path, encoding="utf-8").read()
    assert "data.scraping.sources" in source, "the Settings page lost its source list"
    assert not re.search(r"update\(\s*'scraping'\s*,\s*'sources'", source), \
        "the Settings page can now edit sources — update this note (and the banner copy)"
    assert re.search(r"update\(\s*'scraping'\s*,\s*'live_enabled'", source), \
        "the live-sources switch is what makes no_sources_configured reachable from the UI"


# --------------------------------------------------------------------------- #
# 5. The SPA renders the banner (the five states are component-tested in
#    frontend/src/components/__tests__/EmptyBoardBanner.test.tsx)
# --------------------------------------------------------------------------- #
def test_jobs_page_renders_the_empty_board_banner():
    """The empty branch of the Jobs board is the banner, not a reason-less line.

    A static check in the house style (see ``test_discovery_ai_rescore``'s badge
    test): the component's five states are exercised by the SPA's own vitest
    suite, this only pins that the page still mounts it — and still forwards the
    persona the board is filtered by.
    """
    import os

    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                        "frontend", "src", "pages", "Jobs.tsx"))
    source = open(path, encoding="utf-8").read()
    assert "EmptyBoardBanner" in source, "the Jobs page lost the empty-board banner"
    assert "jobs.length===0" in source or "jobs.length === 0" in source, \
        "the banner must be rendered from the board's own empty branch"
    assert "/api/jobs/discovery/last-run" in source or "useDiscoveryLastRun" in source, \
        "the page must read the last-run endpoint (directly or through the hook)"
