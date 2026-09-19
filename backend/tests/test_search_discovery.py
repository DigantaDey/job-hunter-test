"""Hermetic tests: all provider and page responses are mocked."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import CandidateProfile, Job, SearchBudget, SearchCache, SearchUsage, User
from app.services.search import discover_search, preferences_for_user
from app.services.search.providers import PROVIDERS, BraveSearchProvider, SearchRequest, SearchResult
from app.services.search.queries import SearchPreferences, generate_queries
from app.services.search.store import SearchStore
from app.services.sources.base import SourceError
from app.services.sources.web import candidate_job_url, fetch_page

PREFS = SearchPreferences(("backend engineer",), True)
URL = "https://careers.acme.com/jobs/42"


def job_html(**overrides):
    item = {"@type": "JobPosting", "title": "Backend Engineer", "hiringOrganization": {"name": "Acme"},
            "description": "<p>Authoritative employer description with Python.</p>",
            "datePosted": datetime.utcnow().isoformat(), "validThrough": (datetime.utcnow() + timedelta(days=7)).isoformat()}
    item.update(overrides)
    return '<script type="application/ld+json">' + json.dumps(item) + '</script>'


@pytest.fixture
def user(db):
    row = User(email="search@example.com", name="Secret Person", password_hash="unused")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def search_setup(monkeypatch):
    monkeypatch.setattr(settings, "job_search_providers", "mock")
    monkeypatch.setattr(settings, "job_search_storage_rights", True)
    monkeypatch.setattr(settings, "job_search_queries_per_run", 1)
    calls = []

    class MockProvider:
        id = "mock"
        cost_microusd = 5000

        async def search(self, request):
            calls.append(request)
            return [SearchResult(URL + "?utm_source=search#top"), SearchResult(URL)]

    monkeypatch.setitem(PROVIDERS, "mock", MockProvider)

    async def page(url):
        return url, job_html()

    monkeypatch.setattr("app.services.search.fetch_page", page)
    return calls


def test_query_privacy_fail_closed():
    prefs = SearchPreferences.from_normalized({
        "target_roles": ["Senior Backend Engineer", "Jane Smith", "a@b.com", "555-123-4567", "123 Main Street",
                         "Backend Engineer Jane Smith", "site:private.example.org", "x" * 1000],
        "remote": "remote", "resume": "My entire private CV", "email": "a@b.com", "location": "123 Main St",
        "salary": 250000, "employers": ["Secret Co"],
    })
    assert prefs.roles == ("senior backend engineer",)
    queries = generate_queries(prefs)
    assert len(queries) == 3
    assert all('"senior backend engineer" remote' in q for q in queries)
    assert not any(word in " ".join(queries) for word in ("Jane", "private", "555", "250000", "CV", "@"))
    assert generate_queries(SearchPreferences(("email me at a@b.com",))) == []


async def test_brave_wire_contract_ignores_snippets(monkeypatch):
    monkeypatch.setattr(settings, "brave_search_api_key", "test-key")

    async def request(method, url, **kwargs):
        assert url == "https://api.search.brave.com/res/v1/web/search"
        assert kwargs["headers"]["X-Subscription-Token"] == "test-key"
        assert kwargs["params"] == {"q": '"backend engineer" jobs', "count": 3, "freshness": "pw",
                                    "result_filter": "web", "text_decorations": "false"}
        assert kwargs["retries"] == 0 and kwargs["allow_redirects"] is False
        return SimpleNamespace(status_code=200, json=lambda: {"web": {"results": [
            {"url": URL, "description": "FAKE SNIPPET", "page_age": "2026-09-01T12:00:00Z"}]}})

    monkeypatch.setattr("app.services.http.request", request)
    result = await BraveSearchProvider().search(SearchRequest('"backend engineer" jobs', 3))
    assert result == [SearchResult(URL, datetime(2026, 9, 1, 12))]
    assert not hasattr(result[0], "description")


@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (401, "auth"), (503, "upstream")])
async def test_brave_failure_classification(monkeypatch, status, code):
    monkeypatch.setattr(settings, "brave_search_api_key", "test-key")

    async def request(*args, **kwargs):
        return SimpleNamespace(status_code=status)

    monkeypatch.setattr("app.services.http.request", request)
    with pytest.raises(SourceError) as exc:
        await BraveSearchProvider().search(SearchRequest("jobs", 2))
    assert exc.value.code == code


async def test_validation_dedupe_cache_shared_across_users(db, user, search_setup):
    jobs, report = await discover_search(db, user.id, PREFS)
    assert len(jobs) == len(report["leads"]) == len(search_setup) == 1
    assert jobs[0].description.startswith("Authoritative employer")
    assert report["leads"][0]["status"] == "validated"
    assert report["leads"][0]["attributions"][0]["freshness"] == "unknown"
    other = User(email="other@example.com", name="Other", password_hash="unused")
    db.add(other)
    db.commit()
    _, cached = await discover_search(db, other.id, PREFS)
    assert len(search_setup) == 1
    assert cached["cache_hits"] == 1 and cached["attempts"] == 0
    assert cached["estimated_cost_microusd"] == 0
    usage = db.query(SearchUsage).one()
    assert usage.user_id == user.id and usage.requests == 1
    assert usage.estimated_cost_microusd == 5000 and usage.outcome == "success"
    assert len(usage.query_hash) == 64


async def test_cache_expiry_refetches(db, user, search_setup):
    await discover_search(db, user.id, PREFS)
    db.execute(update(SearchCache).values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
    db.commit()
    await discover_search(db, user.id, PREFS)
    assert len(search_setup) == 2


async def test_no_safe_preferences_no_network(db, user, search_setup):
    _, report = await discover_search(db, user.id, SearchPreferences(("John Doe",)))
    assert report["skipped"] == "no_safe_preferences"
    assert not search_setup and db.query(SearchUsage).count() == 0


async def test_storage_permission_and_configuration(db, user, search_setup, monkeypatch):
    monkeypatch.setattr(settings, "job_search_storage_rights", False)
    _, report = await discover_search(db, user.id, PREFS)
    assert report["skipped"] == "storage_rights_required" and not search_setup
    monkeypatch.setattr(settings, "job_search_storage_rights", True)
    monkeypatch.setattr(settings, "job_search_providers", "google")
    _, report = await discover_search(db, user.id, PREFS)
    assert report["skipped"] == "invalid_provider_config" and not search_setup


async def test_provider_failure_fallback_and_negative_cache(db, user, search_setup, monkeypatch):
    class Broken:
        id = "broken"
        cost_microusd = 1000

        async def search(self, request):
            raise SourceError("secret upstream response", code="rate_limited")

    monkeypatch.setitem(PROVIDERS, "broken", Broken)
    monkeypatch.setattr(settings, "job_search_providers", "broken,mock")
    monkeypatch.setattr(settings, "job_search_queries_per_run", 2)
    jobs, report = await discover_search(db, user.id, PREFS)
    assert jobs and report["attempts"] == 2 and report["estimated_cost_microusd"] == 6000
    assert report["errors"][0] == {"provider": "broken", "code": "rate_limited"}
    assert "secret" not in json.dumps(report)
    assert {r.outcome for r in db.query(SearchUsage)} == {"success", "rate_limited"}
    monkeypatch.setattr(settings, "job_search_queries_per_run", 1)
    _, cached = await discover_search(db, user.id, PREFS)
    assert cached["attempts"] == 0 and cached["cache_hits"] == 2


@pytest.mark.parametrize("setting", ["job_search_user_rpm", "job_search_global_rpm",
                                     "job_search_user_daily", "job_search_global_daily"])
async def test_all_limits_fail_closed(db, user, search_setup, monkeypatch, setting):
    monkeypatch.setattr(settings, setting, 0)
    jobs, report = await discover_search(db, user.id, PREFS)
    assert jobs == [] and report["budget_exhausted"]
    assert not search_setup and db.query(SearchUsage).count() == 0
    assert all(b.used == 0 for b in db.query(SearchBudget))


async def test_single_flight_between_service_instances(db, user, search_setup, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow:
        id = "mock"
        cost_microusd = 5000

        async def search(self, request):
            entered.set()
            await release.wait()
            return [SearchResult(URL)]

    monkeypatch.setitem(PROVIDERS, "mock", Slow)
    task = asyncio.create_task(discover_search(db, user.id, PREFS))
    await entered.wait()
    with Session(db.get_bind()) as other:
        jobs, report = await discover_search(other, user.id, PREFS)
    assert not jobs and report["coalesced"] == 1 and report["attempts"] == 0
    release.set()
    await task
    assert db.query(SearchUsage).count() == 1


def test_expired_lease_recovery_and_old_owner_fencing(db):
    store = SearchStore(db.get_bind())
    old, _ = store.claim("key")
    db.execute(update(SearchCache).values(lease_until=datetime.utcnow() - timedelta(seconds=1)))
    db.commit()
    new, _ = store.claim("key")
    assert old != new and new not in ("hit", "busy")
    store.finish("key", new, {"results": []}, 100)
    store.finish("key", old, {"error": "stale"}, 100)
    assert store.claim("key") == ("hit", {"results": []})


async def test_global_quota_shared_between_users(db, user, search_setup, monkeypatch):
    monkeypatch.setattr(settings, "job_search_global_daily", 1)
    await discover_search(db, user.id, PREFS)
    other = User(email="other@example.com", password_hash="unused")
    db.add(other)
    db.commit()
    _, report = await discover_search(db, other.id, SearchPreferences(("data engineer",)))
    assert report["budget_exhausted"] and len(search_setup) == 1


async def test_career_page_to_job_with_bounded_fetches(db, user, search_setup, monkeypatch):
    calls = []

    async def page(url):
        calls.append(url)
        if url == URL:
            return url, '<a href="/jobs/43">Opening</a><a href="https://evil.com/jobs/1">Ignore</a>'
        return url, job_html()

    monkeypatch.setattr("app.services.search.fetch_page", page)
    jobs, report = await discover_search(db, user.id, PREFS)
    assert len(calls) == 2 and jobs[0].url.endswith("/43")
    assert report["leads"][0]["status"] == "discovery_lead"
    assert jobs[0].extra["search_discovery"]["attributions"][0]["career_page"] == URL
    monkeypatch.setattr(settings, "job_search_fetch_limit", 1)
    jobs, report = await discover_search(db, user.id, PREFS)
    assert jobs == [] and report["pages_fetched"] == 1


@pytest.mark.parametrize("html", ["<h1>Search snippet claims we are hiring!</h1>",
                                  job_html(validThrough="2001-01-01"), job_html(description=""),
                                  job_html(hiringOrganization={}), job_html(validThrough="unknown"),
                                  job_html(datePosted="2001-01-01")])
async def test_unvalidated_expired_stale_never_become_jobs(db, user, search_setup, monkeypatch, html):
    async def page(url):
        return url, html

    monkeypatch.setattr("app.services.search.fetch_page", page)
    jobs, report = await discover_search(db, user.id, PREFS)
    assert jobs == [] and report["leads"][0]["status"] == "discovery_lead"


async def test_unknown_posted_at_not_invented(db, user, search_setup, monkeypatch):
    async def page(url):
        return url, job_html(datePosted=None)

    monkeypatch.setattr("app.services.search.fetch_page", page)
    jobs, _ = await discover_search(db, user.id, PREFS)
    assert jobs[0].posted_at is None


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/job", "http://169.254.169.254/",
                                 "https://a:b@example.com/", "https://example.com:5432/", "https://localhost/jobs",
                                 "https://example.com\\@evil.com/", "javascript:alert(1)"])
def test_reject_unsafe_result_urls(url):
    assert candidate_job_url(url) is None


async def test_fetch_redirects_ssrf_and_robots_per_hop(monkeypatch):
    checked, requested = [], []

    async def guard(url, **kwargs):
        assert kwargs == {"allow_private": False, "allowlist": []}
        checked.append(url)

    async def request(method, url, **kwargs):
        requested.append(url)
        assert kwargs["respect_robots"] is True and kwargs["allow_redirects"] is False
        if len(requested) == 1:
            return SimpleNamespace(status_code=302, headers={"location": "/jobs/43"})
        return SimpleNamespace(status_code=200, headers={"content-type": "text/html"}, text=job_html())

    monkeypatch.setattr("app.services.sources.web.check_url", guard)
    monkeypatch.setattr("app.services.http.request", request)
    url, _ = await fetch_page(URL)
    assert url.endswith("43") and checked == requested and len(checked) == 2


async def test_fetch_blocks_private_redirect_before_request(monkeypatch):
    async def guard(*args, **kwargs):
        pass

    async def request(*args, **kwargs):
        return SimpleNamespace(status_code=302, headers={"location": "http://169.254.169.254/"})

    monkeypatch.setattr("app.services.sources.web.check_url", guard)
    monkeypatch.setattr("app.services.http.request", request)
    with pytest.raises(ValueError, match="Unsafe redirect"):
        await fetch_page(URL)


async def test_discovery_integration_persists_only_validated(db, user, search_setup, monkeypatch):
    from app.services.discovery import discover_for_user

    db.add(CandidateProfile(user_id=user.id, state="active", is_current=True,
                            document={"preferences": {"target_roles": ["Backend Engineer"], "remote": "remote"},
                                      "contact": {"email": "secret@example.com"}}))
    db.commit()

    async def direct(*args, **kwargs):
        return [], {"requested": [], "ok": {}, "errors": {}, "total": 0}

    async def form(*args, **kwargs):
        return {"fields": []}

    monkeypatch.setattr("app.services.sources.fetch_all", direct)
    monkeypatch.setattr("app.services.discovery.detect_form_structure", form)
    result = await discover_for_user(db, user, keywords=["python"], freshness_hours=168, live_enabled=True)
    assert result["inserted"] == result["search"]["validated"] == 1
    row = db.query(Job).one()
    assert row.extra["search_discovery"]["status"] == "validated"
    assert row.raw_payload["search_discovery"]["attributions"][0]["provider"] == "mock"
    assert "secret" not in search_setup[0].query
    second = await discover_for_user(db, user, keywords=["python"], freshness_hours=168, live_enabled=True)
    assert second["inserted"] == 0 and db.query(Job).count() == 1


def test_preferences_tenant_scoped_and_only_active(db, user):
    db.add(CandidateProfile(user_id=user.id, state="draft", is_current=True,
                            document={"preferences": {"target_roles": ["backend engineer"]}}))
    db.commit()
    assert preferences_for_user(db, user.id, None).roles == ()
    assert preferences_for_user(db, 98765, None).roles == ()


async def test_explicit_robots_check_overrides_global_disabled(monkeypatch):
    from urllib.robotparser import RobotFileParser

    from app.services import robots

    monkeypatch.setattr(settings, "respect_robots_txt", False)
    robots.clear_cache()

    async def blocked(url):
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /"])
        return parser

    monkeypatch.setattr(robots, "_load", blocked)
    assert await robots.can_fetch(URL) is True
    assert await robots.can_fetch(URL, force=True) is False
    robots.clear_cache()


async def test_direct_sources_survive_search_failure(db, user, search_setup, monkeypatch):
    from app.services.discovery import discover_for_user
    from app.services.sources.base import Posting

    async def direct(*args, **kwargs):
        return [Posting(title="Engineer", company="Direct", url=URL, source="lever", description="Python")], {
            "requested": ["lever"], "ok": {"lever": 1}, "errors": {}, "total": 1}

    async def broken(*args, **kwargs):
        raise RuntimeError("private provider text")

    async def form(*args, **kwargs):
        return {"fields": []}

    monkeypatch.setattr("app.services.sources.fetch_all", direct)
    monkeypatch.setattr("app.services.search.discover_search", broken)
    monkeypatch.setattr("app.services.discovery.detect_form_structure", form)
    result = await discover_for_user(db, user, keywords=["python"], freshness_hours=168, live_enabled=True)
    assert result["inserted"] == 1 and result["search"]["validated"] == 0
    assert "private" not in json.dumps(result)


async def test_disabled_live_fetch_never_searches(db, user, search_setup):
    from app.services.discovery import discover_for_user

    result = await discover_for_user(db, user, keywords=["python"], freshness_hours=168, live_enabled=False)
    assert result["inserted"] == 0 and not search_setup


async def test_already_fetched_url_not_fetched_again(db, user, search_setup, monkeypatch):
    async def page(url):
        pytest.fail("Duplicate URL was fetched")

    monkeypatch.setattr("app.services.search.fetch_page", page)
    jobs, report = await discover_search(db, user.id, PREFS, known_urls=[URL])
    assert jobs == [] and report["pages_fetched"] == 0


async def test_empty_results_are_cached(db, user, search_setup, monkeypatch):
    calls = []

    class Empty:
        id = "mock"
        cost_microusd = 5000

        async def search(self, request):
            calls.append(request)
            return []

    monkeypatch.setitem(PROVIDERS, "mock", Empty)
    await discover_search(db, user.id, PREFS)
    jobs, report = await discover_search(db, user.id, PREFS)
    assert not jobs and len(calls) == 1 and report["cache_hits"] == 1


async def test_private_dns_answer_never_fetched(monkeypatch):
    from app.services import net_guard

    async def resolve(host, port):
        return ["127.0.0.1"]

    async def request(*args, **kwargs):
        pytest.fail("Private URL reached HTTP")

    net_guard.clear_dns_cache()
    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr("app.services.http.request", request)
    with pytest.raises(net_guard.OutboundURLBlocked):
        await fetch_page(URL)


def test_atomic_global_quota_concurrent_workers(db, user, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services.search.store import BudgetExceeded

    monkeypatch.setattr(settings, "job_search_global_daily", 1)
    store = SearchStore(db.get_bind())
    barrier = Barrier(2)
    uid = user.id

    def reserve(index):
        barrier.wait(timeout=5)
        try:
            return store.reserve(uid, "mock", str(index), 5000)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [1, 2]))
    assert sum(r is not None for r in results) == 1
    assert db.query(SearchUsage).count() == 1
    assert all(row.used == 1 for row in db.query(SearchBudget))


def test_search_migration_round_trip(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + str(tmp_path / "migrations.db"))
    command.upgrade(config, "head")
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    try:
        assert {"search_usage", "search_budgets", "search_cache"} <= set(inspect(engine).get_table_names())
        command.downgrade(config, "d2e3f4a5b6c7")
        assert "search_usage" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert "search_usage" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
