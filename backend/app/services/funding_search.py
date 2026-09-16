"""
Web-search backend for the funding radar (v2.2.6).

Why this exists
---------------
The direct SEC EDGAR path fetches ``https://efts.sec.gov/LATEST/search-index``,
and ``efts.sec.gov``'s robots.txt disallows automated access to it. With
``RESPECT_ROBOTS_TXT=true`` (the default) that made every funding scan fail with
``PermissionError: robots.txt disallows …`` — an honest verdict, but a dead
radar. When a search engine is configured, scans discover funding events
through the search API instead and never touch ``efts.sec.gov`` at all; the
direct EDGAR path remains the verified fallback **when no search provider is
configured** (the robots check is never disabled for it).

Interface
---------
:func:`search` is the single entry point used by the funding radar::

    search(query, *, window_days, limit)
        -> [{"title": str, "url": str, "snippet": str, "published_at": str|None}, …]

It never returns companies or funding events — those are extracted from the
snippets by the AI pass in :mod:`app.services.funding_sources`
(``ai_extract_events_from_results``), which grounds every event in the cited
result's own text. This module only finds candidate pages.

Providers
---------

* ``tavily`` — the Tavily search API (``FUNDING_SEARCH_API_KEY``). The request
  is an authenticated API call (an RPC, not a crawl): it is *not* subject to
  robots.txt, but it still goes through the shared HTTP client, so the SSRF
  guard (:mod:`app.services.net_guard`) vets every hop.
* ``stub``   — a deterministic, hermetic fixture (no network) for tests and
  local development. Its rows are clearly synthetic: events extracted from
  them are labelled ``verified=False`` by the extraction pass, exactly like
  the demo dataset.

Configuration (single source of truth:
:func:`app.core.config.resolve_funding_search_provider`):

* ``FUNDING_SEARCH_PROVIDER=tavily|stub`` (empty/``auto``: ``tavily`` when a
  key is set, else the direct sec_edgar path);
* ``FUNDING_SEARCH_API_KEY``.

Every failure is raised, never swallowed: the funding radar turns it into a
per-provider entry in the scan report (``provider_errors.search``), so a broken
search key looks the same as any other provider outage — never like an empty
market.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc

log = get_logger("app.funding_search")

#: Tavily search API. Documented request shape: POST with ``api_key``,
#: ``query``, ``topic=news``, ``days`` (recency window) and ``max_results``.
TAVILY_ENDPOINT = "https://api.tavily.com/search"

#: Rows returned per :func:`search` call are bounded by the caller's limit,
#: with a hard ceiling so a misconfigured limit cannot balloon the prompt
#: that the extraction pass receives.
MAX_SEARCH_RESULTS = 20


class FundingSearchError(RuntimeError):
    """The search provider could not answer (transport failure or bad status).

    ``status`` is the provider's HTTP status when there was one; ``None`` means
    a transport-level failure. The funding provider wraps this into a
    ``FundingProviderError`` so 4xx answers are never retried.
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def search_provider() -> str:
    """The resolved provider id (``tavily`` | ``stub``), ``""`` when off."""
    return settings.funding_search_effective


def search_configured() -> bool:
    """True when a usable search provider is configured for funding scans."""
    return bool(search_provider())


def search_hint() -> str:
    """Operator-facing setup hint (``""`` when nothing is missing)."""
    raw = (settings.funding_search_provider or "").strip().lower()
    key = (settings.funding_search_api_key or "").strip()
    if raw in ("tavily",) and not key and search_provider() != "tavily":
        return "FUNDING_SEARCH_PROVIDER=tavily needs FUNDING_SEARCH_API_KEY"
    if raw and raw not in ("", "auto", "stub", "tavily"):
        return (f"FUNDING_SEARCH_PROVIDER='{raw}' is not one of "
                f"tavily|stub — the search path stays off")
    if not raw and not key:
        return ("Set FUNDING_SEARCH_PROVIDER=tavily and FUNDING_SEARCH_API_KEY to scan via a "
                "web search API — without it scans use direct SEC EDGAR, which efts.sec.gov's "
                "robots.txt may block (scan_failed with provider_errors.sec_edgar)")
    return ""


async def search(query: str, window_days: int = 45, limit: int = 8) -> List[Dict[str, Any]]:
    """Search the web for recent funding news; returns result rows.

    Each row is ``{"title", "url", "snippet", "published_at"}`` with
    ``published_at`` an ISO string when the provider reports one, else ``None``.
    Raises :class:`FundingSearchError` when the provider fails — never returns
    a quiet empty list for an outage (an *honest* empty result set is the
    provider answering 200 with zero rows).
    """
    text = " ".join(str(query or "").split())
    if not text:
        return []
    provider = search_provider()
    if not provider:
        raise FundingSearchError("no funding search provider is configured")
    limit = max(0, min(MAX_SEARCH_RESULTS, int(limit)))
    if provider == "tavily":
        rows = await _search_tavily(text, window_days, limit)
    elif provider == "stub":
        rows = _search_stub(text, window_days, limit)
    else:
        raise FundingSearchError(f"unknown funding search provider '{provider}'")
    normalised = [_normalise_row(row) for row in rows]
    return [row for row in normalised if row["url"] or row["snippet"]][:limit]


def _normalise_row(row: Any) -> Dict[str, Any]:
    """One provider row → the documented shape (bad rows become empty ones)."""
    if not isinstance(row, dict):
        row = {}
    published = row.get("published_at") or row.get("published_date")
    return {
        "title": str(row.get("title") or "").strip()[:300],
        "url": str(row.get("url") or "").strip()[:500],
        "snippet": str(row.get("snippet") or row.get("content") or "").strip()[:1000],
        "published_at": (str(published).strip()[:40] if published else None),
    }


# --------------------------------------------------------------------------- #
# tavily
# --------------------------------------------------------------------------- #
async def _search_tavily(query: str, window_days: int, limit: int) -> List[Dict[str, Any]]:
    """Tavily search API (news topic, recency window = the scan's window)."""
    key = (settings.funding_search_api_key or "").strip()
    if not key:
        raise FundingSearchError("FUNDING_SEARCH_PROVIDER=tavily but FUNDING_SEARCH_API_KEY is empty")
    from app.services import http as http_client

    response = await http_client.request(
        "POST", TAVILY_ENDPOINT,
        json_body={
            "query": query,
            "topic": "news",
            "days": max(1, int(window_days)),
            "max_results": max(1, limit),
            "search_depth": "basic",
            "include_answer": False,
        },
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=settings.funding_provider_timeout_seconds,
        # An authenticated search API is an RPC, not a crawl: robots.txt governs
        # crawling a site's published pages, and this call fetches JSON we are
        # licensed (by the API key) to read. The SSRF guard still applies —
        # net_guard vets the host on every hop of the shared client.
        respect_robots=False,
    )
    if response.status_code != 200:
        raise FundingSearchError(f"tavily returned HTTP {response.status_code}",
                                 status=response.status_code)
    try:
        payload = response.json()
    except Exception as exc:  # not JSON is a provider fault, not an empty market
        raise FundingSearchError(f"tavily returned a body that is not JSON: {exc}") from exc
    rows: List[Dict[str, Any]] = []
    for row in (payload.get("results") or [])[:limit]:
        if isinstance(row, dict):
            rows.append({
                "title": row.get("title") or "",
                "url": row.get("url") or "",
                "snippet": row.get("content") or "",
                "published_at": row.get("published_date"),
            })
    inc("jobhunter_funding_search_results_total", provider="tavily", value=len(rows))
    return rows


# --------------------------------------------------------------------------- #
# stub — deterministic, hermetic (no network). Tests/dev only.
# --------------------------------------------------------------------------- #
#: A small fixed corpus shaped like real search hits. It deliberately ignores
#: the query: the stub exists so the *whole* search workflow (queries →
#: results → AI extraction with citations → ranked radar) runs hermetically,
#: not to mimic a real index. Events extracted from these rows are labelled
#: ``verified=False`` — synthetic fixture data must never look authoritative.
_STUB_RESULTS: Tuple[Dict[str, Any], ...] = (
    {
        "title": "VectorLoom AI raises $12M Series A to scale vector search infrastructure",
        "url": "https://example.com/press/vectorloom-series-a",
        "snippet": ("VectorLoom AI announced a $12 million Series A round led by Northgate "
                    "Ventures to grow its vector database platform and expand engineering."),
        "days_ago": 4,
    },
    {
        "title": "Nightowl Robotics secures $6.5M Seed round for warehouse autonomy",
        "url": "https://example.com/news/nightowl-seed",
        "snippet": ("Nightowl Robotics closed a $6.5 million Seed round; the startup says the "
                    "capital funds its warehouse autonomy rollout over the next year."),
        "days_ago": 11,
    },
    {
        "title": "Cobalt Health announces $20M Series B expansion",
        "url": "https://example.com/news/cobalt-health-series-b",
        "snippet": ("Cobalt Health raised a $20 million Series B led by Meridian Growth to "
                    "expand its care-coordination platform into three new markets."),
        "days_ago": 20,
    },
)


def _search_stub(query: str, window_days: int, limit: int) -> List[Dict[str, Any]]:
    """Deterministic fixture rows within the freshness window (no network)."""
    now = datetime.utcnow()
    cutoff = now - timedelta(days=max(1, int(window_days)))
    rows: List[Dict[str, Any]] = []
    for row in _STUB_RESULTS:
        published = now - timedelta(days=int(row["days_ago"]))
        if published < cutoff:
            continue
        rows.append({
            "title": row["title"],
            "url": row["url"],
            "snippet": row["snippet"],
            "published_at": published.replace(microsecond=0).isoformat() + "Z",
        })
        if len(rows) >= max(1, limit):
            break
    inc("jobhunter_funding_search_results_total", provider="stub", value=len(rows))
    return rows


__all__ = [
    "FundingSearchError",
    "MAX_SEARCH_RESULTS",
    "TAVILY_ENDPOINT",
    "search",
    "search_configured",
    "search_hint",
    "search_provider",
]
