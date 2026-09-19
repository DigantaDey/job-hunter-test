"""Search discovery orchestration; only validated source postings leave here."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import CandidateProfile
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


async def discover_search(db: Session, user_id: int, preferences: SearchPreferences, *,
                          since_hours: int = 168, known_urls=()) -> tuple[list[Posting], dict]:
    report: dict[str, Any] = {"attempts": 0, "cache_hits": 0, "coalesced": 0, "errors": [], "leads": [],
              "validated": 0, "estimated_cost_microusd": 0}
    if not settings.job_search_providers.strip():
        report["skipped"] = "disabled"
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
    for query in queries:
        request = SearchRequest(query, settings.job_search_results_per_query, freshness)
        for provider in providers:
            # Account-independent, shareable ONLY because queries contain a
            # public curated vocabulary; no user/profile/resume in the key.
            key = hashlib.sha256(json.dumps({"v": 1, "provider": provider.id, **asdict(request)},
                                             sort_keys=True).encode()).hexdigest()
            owner, payload = store.claim(key)
            if owner == "busy":
                report["coalesced"] += 1
                # Do not bypass an in-flight primary with a paid fallback.
                break
            if owner == "hit":
                report["cache_hits"] += 1
            else:
                if report["attempts"] >= settings.job_search_queries_per_run:
                    store.finish(key, owner, {}, 0)
                    report["budget_exhausted"] = True
                    break
                try:
                    usage_id = store.reserve(user_id, provider.id, key, provider.cost_microusd)
                except BudgetExceeded:
                    store.finish(key, owner, {}, 0)
                    report["budget_exhausted"] = True
                    break
                report["attempts"] += 1
                report["estimated_cost_microusd"] += provider.cost_microusd
                try:
                    rows = await asyncio.wait_for(provider.search(request), settings.job_search_timeout_seconds)
                    retrieved = datetime.utcnow().isoformat()
                    payload = {"results": [{"url": r.url, "page_date": r.page_date.isoformat() if r.page_date else None}
                                           for r in rows[:request.count] if isinstance(r, SearchResult)],
                               "retrieved_at": retrieved}
                    store.outcome(usage_id, "success")
                    store.finish(key, owner, payload, settings.job_search_cache_seconds)
                except Exception as exc:
                    # Never persist exception text (may contain API keys, query,
                    # provider response bodies); short negative cache prevents
                    # repeated charges during an outage.
                    code = exc.code if isinstance(exc, SourceError) else "timeout" if isinstance(exc, TimeoutError) else "upstream"
                    payload = {"error": code}
                    store.outcome(usage_id, code)
                    store.finish(key, owner, payload, 30)
            if payload.get("error"):
                report["errors"].append({"provider": provider.id, "code": payload["error"]})
                continue  # next configured provider; direct sources always survive
            for row in payload.get("results", []):
                url = candidate_job_url(row.get("url", ""))
                if not url or url in visited:
                    continue
                lead = leads.setdefault(url, DiscoveryLead(url))
                attribution = {"provider": provider.id, "query_hash": key, "result_url": url,
                               "retrieved_at": payload.get("retrieved_at"), "page_date": row.get("page_date"),
                               "freshness": "provider_reported" if row.get("page_date") else "unknown"}
                if attribution not in lead.attributions:
                    lead.attributions.append(attribution)
            break
    # A single total page budget includes career pages and their child links.
    queue = [(lead, 0) for lead in leads.values()]
    source = WebPageSource()
    postings: dict[str, Posting] = {}
    fetched = 0
    while queue and fetched < settings.job_search_fetch_limit:
        lead, depth = queue.pop(0)
        if lead.url in visited:
            continue
        visited.add(lead.url)
        fetched += 1
        try:
            final_url, html = await asyncio.wait_for(fetch_page(lead.url), settings.job_search_timeout_seconds)
            normalized, links = source.normalize_page(final_url, html)
            if final_url != lead.url and final_url in visited:
                lead.status = "duplicate"
                continue
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
                for link in links:
                    if link not in leads and link not in visited:
                        child = DiscoveryLead(link, [dict(a, career_page=lead.url) for a in lead.attributions])
                        leads[link] = child
                        queue.append((child, depth + 1))
        except Exception:
            # Blocked, malformed, gated and unavailable pages remain unvalidated.
            pass
    report["leads"] = [asdict(lead) for lead in leads.values()]
    report["pages_fetched"] = fetched
    report["validated"] = len(postings)
    return list(postings.values()), report
