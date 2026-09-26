"""Search discovery orchestration; only validated source postings leave here."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import CandidateProfile
from app.services.reliability import count, duration, note_dedupe
from app.services.search.providers import SearchRequest, SearchResult, configured_providers
from app.services.search.queries import SearchPreferences, generate_queries
from app.services.search.store import BudgetExceeded, SearchStore
from app.services.sources.base import Posting, SourceError
from app.services.sources.web import WebPageSource, candidate_job_url, fetch_page


@dataclass
class DiscoveryLead:
    url: str
    attributions: list[dict] = field(default_factory=list)
    status: str = "discovery_lead"


def preferences_for_user(db: Session, user_id: int, persona_id: int | None) -> SearchPreferences:
    profile = db.query(CandidateProfile).filter(
        CandidateProfile.user_id == user_id, CandidateProfile.persona_id == persona_id,
        CandidateProfile.is_current.is_(True), CandidateProfile.state == "active",
    ).order_by(CandidateProfile.version.desc()).first()
    preferences = (profile.document or {}).get("preferences", {}) if profile else {}
    return SearchPreferences.from_normalized(preferences if isinstance(preferences, dict) else {})


#: How many search units one run keeps in flight (v2.4).
#:
#: Queries × providers used to run in nested ``for`` loops with one ``await``
#: each, and the validation crawl was a ``while`` queue fetching one page at a
#: time — three queries × a 15 s provider cap plus up to
#: ``JOB_SEARCH_FETCH_LIMIT`` (12) validated pages is ~3 minutes of *serial*
#: waiting added before discovery even starts scoring. Nothing needs to be
#: serial except politeness, and the HTTP client already spaces requests per
#: host (``PER_HOST_MIN_INTERVAL_SECONDS``) behind a global
#: ``MAX_CONCURRENT_FETCHES`` semaphore — so a bounded gather cuts the wall clock
#: without touching a single limit.
_SEARCH_CONCURRENCY = 4


async def discover_search(db: Session, user_id: int, preferences: SearchPreferences, *,
                          since_hours: int = 168, known_urls=()) -> tuple[list[Posting], dict]:
    """Search-provider discovery: queries, then validate the leads they return.

    Two stages, both **bounded-parallel** since v2.4 (``_SEARCH_CONCURRENCY``):

    * queries run concurrently, each walking the configured providers *in order*
      — the first provider that answers is the one the deployment pays for, so
      racing the provider chain itself would pay every provider for every query;
    * the validation crawl runs in waves of up to four pages, keeping the same
      global page budget (``JOB_SEARCH_FETCH_LIMIT``) and the same FIFO order:
      a career page discovered by this run's first query is still validated
      before the second query's leads.

    The report is assembled in query order from per-query fragments, so it reads
    exactly as it did when the loop was serial.
    """
    report: dict[str, Any] = {"attempts": 0, "cache_hits": 0, "coalesced": 0, "errors": [], "leads": [],
              "validated": 0, "estimated_cost_microusd": 0}
    if not settings.job_search_providers.strip():
        report["skipped"] = "disabled"
        count("jobhunter_search_provider_requests_total", provider="other", outcome="skipped")
        return [], report
    if not settings.job_search_storage_rights:
        report["skipped"] = "storage_rights_required"
        return [], report
    try:
        providers = configured_providers()
    except ValueError:
        report["skipped"] = "invalid_provider_config"
        return [], report
    queries = generate_queries(preferences)[:settings.job_search_queries_per_run]
    if not queries:
        report["skipped"] = "no_safe_preferences"
        return [], report
    store = SearchStore(db.get_bind())
    leads: dict[str, DiscoveryLead] = {}
    visited = {url for value in known_urls if (url := candidate_job_url(value))}
    freshness = "pd" if since_hours <= 24 else "pw" if since_hours <= 168 else "pm" if since_hours <= 744 else "py"
    # A query that *starts* must be able to afford its whole provider chain,
    # otherwise a failing primary provider burns the run's attempt budget on
    # every query's first call and no query ever reaches its fallback — the
    # serial loop concentrated the budget on the first query instead, which is
    # what found leads at all. So the in-flight ceiling is the run's attempt
    # budget divided by the chain length: with the deployed single provider that
    # is the full ``_SEARCH_CONCURRENCY`` (the common case, and the one this
    # fixes); with two configured providers it degrades to the old serial order
    # rather than spending the budget on failures.
    chain_length = max(1, len(providers))
    concurrency = max(1, min(_SEARCH_CONCURRENCY, len(queries),
                             settings.job_search_queries_per_run // chain_length or 1))
    semaphore = asyncio.Semaphore(concurrency)
    #: Attempts are a *run-level* budget (``JOB_SEARCH_QUERIES_PER_RUN`` paid
    #: calls), so the counter is shared and checked under a lock — otherwise two
    #: concurrent queries could both see the last free attempt.
    budget_lock = asyncio.Lock()
    attempts_used = 0

    async def run_query(query: str) -> dict:
        """One query across the providers, in configured order."""
        nonlocal attempts_used
        fragment: dict[str, Any] = {"attempts": 0, "cache_hits": 0, "coalesced": 0,
                                    "errors": [], "budget_exhausted": False,
                                    "cost_microusd": 0, "leads": {}}
        request = SearchRequest(query, settings.job_search_results_per_query, freshness)
        async with semaphore:
            for provider in providers:
                # Account-independent, shareable ONLY because queries contain a
                # public curated vocabulary; no user/profile/resume in the key.
                key = hashlib.sha256(json.dumps({"v": 1, "provider": provider.id, **asdict(request)},
                                                 sort_keys=True).encode()).hexdigest()
                owner, payload = store.claim(key)
                if owner == "busy":
                    fragment["coalesced"] += 1
                    # Two runs asked for the same query at the same moment: the
                    # second one waits on the first instead of paying twice.
                    note_dedupe("search_cache")
                    count("jobhunter_search_provider_requests_total", provider=provider.id,
                          outcome="coalesced")
                    # Do not bypass an in-flight primary with a paid fallback.
                    break
                if owner == "hit":
                    fragment["cache_hits"] += 1
                    note_dedupe("search_cache")
                    count("jobhunter_search_provider_requests_total", provider=provider.id,
                          outcome="cached")
                else:
                    async with budget_lock:
                        # The run-level attempt budget and the provider quota are
                        # both reserved here, before the call is made.
                        if attempts_used >= settings.job_search_queries_per_run:
                            store.finish(key, owner, {}, 0)
                            fragment["budget_exhausted"] = True
                            break
                        try:
                            usage_id = store.reserve(user_id, provider.id, key, provider.cost_microusd)
                        except BudgetExceeded:
                            store.finish(key, owner, {}, 0)
                            fragment["budget_exhausted"] = True
                            count("jobhunter_search_provider_requests_total", provider=provider.id,
                                  outcome="budget_exceeded")
                            break
                        attempts_used += 1
                    fragment["attempts"] += 1
                    fragment["cost_microusd"] += provider.cost_microusd
                    count("jobhunter_search_provider_cost_microusd_total",
                          value=float(provider.cost_microusd), provider=provider.id)
                    call_started = time.perf_counter()
                    try:
                        rows = await asyncio.wait_for(provider.search(request), settings.job_search_timeout_seconds)
                        duration("jobhunter_search_provider_duration_seconds",
                                 time.perf_counter() - call_started, provider=provider.id)
                        retrieved = datetime.utcnow().isoformat()
                        payload = {"results": [{"url": r.url, "page_date": r.page_date.isoformat() if r.page_date else None}
                                               for r in rows[:request.count] if isinstance(r, SearchResult)],
                                   "retrieved_at": retrieved}
                        store.outcome(usage_id, "success")
                        store.finish(key, owner, payload, settings.job_search_cache_seconds)
                        count("jobhunter_search_provider_requests_total", provider=provider.id,
                              outcome="success")
                    except Exception as exc:
                        # Never persist exception text (may contain API keys, query,
                        # provider response bodies); short negative cache prevents
                        # repeated charges during an outage.
                        code = exc.code if isinstance(exc, SourceError) else "timeout" if isinstance(exc, TimeoutError) else "upstream"
                        payload = {"error": code}
                        store.outcome(usage_id, code)
                        store.finish(key, owner, payload, 30)
                        duration("jobhunter_search_provider_duration_seconds",
                                 time.perf_counter() - call_started, provider=provider.id)
                        count("jobhunter_search_provider_requests_total", provider=provider.id,
                              outcome="error")
                if payload.get("error"):
                    fragment["errors"].append({"provider": provider.id, "code": payload["error"]})
                    continue  # next configured provider; direct sources always survive
                for row in payload.get("results", []):
                    url = candidate_job_url(row.get("url", ""))
                    if not url or url in visited:
                        continue
                    attribution = {"provider": provider.id, "query_hash": key, "result_url": url,
                                   "retrieved_at": payload.get("retrieved_at"), "page_date": row.get("page_date"),
                                   "freshness": "provider_reported" if row.get("page_date") else "unknown"}
                    attributions = fragment["leads"].setdefault(url, [])
                    if attribution not in attributions:
                        attributions.append(attribution)
                break
        return fragment

    fragments = await asyncio.gather(*(run_query(query) for query in queries))
    # Merged in query order, so the report and the crawl queue read exactly as
    # they did when the loop was serial.
    for fragment in fragments:
        report["attempts"] += int(fragment["attempts"])
        report["cache_hits"] += int(fragment["cache_hits"])
        report["coalesced"] += int(fragment["coalesced"])
        report["estimated_cost_microusd"] += int(fragment["cost_microusd"])
        report["errors"].extend(fragment["errors"])
        if fragment["budget_exhausted"]:
            report["budget_exhausted"] = True
        lead_attributions: Dict[str, List[Dict[str, Any]]] = fragment.get("leads") or {}
        for url, attributions in lead_attributions.items():
            if not url:
                continue
            lead = leads.setdefault(url, DiscoveryLead(url))
            for attribution in attributions:
                if attribution not in lead.attributions:
                    lead.attributions.append(attribution)

    # A single total page budget includes career pages and their child links.
    # Fetched in waves of up to ``_SEARCH_CONCURRENCY``; the budget, the visited
    # set and the FIFO order are all unchanged.
    queue = [(lead, 0) for lead in leads.values()]
    source = WebPageSource()
    postings: dict[str, Posting] = {}
    fetched = 0

    async def crawl(lead: DiscoveryLead, depth: int) -> list[DiscoveryLead]:
        """Fetch and normalise one lead; return the child leads it revealed."""
        try:
            final_url, html = await asyncio.wait_for(fetch_page(lead.url), settings.job_search_timeout_seconds)
            normalized, links = source.normalize_page(final_url, html)
            if final_url != lead.url and final_url in visited:
                lead.status = "duplicate"
                return []
            visited.add(final_url)
            for posting in normalized:
                # Index timestamps and retrieval times never become posted_at.
                if posting.posted_at and (datetime.utcnow() - posting.posted_at).total_seconds() > since_hours * 3600:
                    continue
                posting.extra["search_discovery"] = {"status": "validated", "attributions": lead.attributions,
                                                     "validated_at": datetime.utcnow().isoformat(),
                                                     "validation": "schema.org/JobPosting"}
                posting.raw["search_discovery"] = posting.extra["search_discovery"]
                postings.setdefault(posting.canonical_id(), posting)
                lead.status = "validated"
            if depth == 0 and not normalized:
                children: list[DiscoveryLead] = []
                for link in links:
                    if link not in leads and link not in visited:
                        child = DiscoveryLead(link, [dict(a, career_page=lead.url) for a in lead.attributions])
                        leads[link] = child
                        children.append(child)
                return children
        except Exception:
            # Blocked, malformed, gated and unavailable pages remain unvalidated.
            pass
        return []

    while queue and fetched < settings.job_search_fetch_limit:
        wave: list[tuple[DiscoveryLead, int]] = []
        room = settings.job_search_fetch_limit - fetched
        while queue and len(wave) < concurrency and len(wave) < room:
            lead, depth = queue.pop(0)
            if lead.url in visited:
                continue
            visited.add(lead.url)
            wave.append((lead, depth))
        if not wave:
            break
        # The budget is spent when the wave is *scheduled*, not when it returns:
        # a page that is on the wire has been fetched for accounting purposes.
        fetched += len(wave)
        results = await asyncio.gather(*(crawl(lead, depth) for lead, depth in wave),
                                       return_exceptions=True)
        for (_lead, depth), children in zip(wave, results, strict=False):
            if isinstance(children, BaseException):  # pragma: no cover - crawl swallows
                continue
            queue.extend((child, depth + 1) for child in children)

    report["leads"] = [asdict(lead) for lead in leads.values()]
    report["pages_fetched"] = fetched
    report["validated"] = len(postings)
    return list(postings.values()), report
