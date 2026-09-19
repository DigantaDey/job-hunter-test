"""Replaceable search boundary. Inputs contain no user identifier or resume."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.config import settings
from app.services import http
from app.services.sources.base import SourceError, parse_datetime


@dataclass(frozen=True)
class SearchRequest:
    query: str
    count: int
    freshness: str = "pw"


@dataclass(frozen=True)
class SearchResult:
    url: str
    page_date: datetime | None = None
    # Intentionally no description/snippet field: not job facts.


class SearchProvider(Protocol):
    id: str
    @property
    def cost_microusd(self) -> int: ...

    async def search(self, request: SearchRequest) -> list[SearchResult]: ...


class BraveSearchProvider:
    id = "brave"

    @property
    def cost_microusd(self) -> int:
        return settings.brave_search_cost_microusd

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        if not settings.brave_search_api_key:
            raise SourceError("Search credentials missing", code="auth")
        response = await http.request(
            "GET", "https://api.search.brave.com/res/v1/web/search",
            params={"q": request.query, "count": request.count, "freshness": request.freshness,
                    "result_filter": "web", "text_decorations": "false"},
            headers={"Accept": "application/json", "X-Subscription-Token": settings.brave_search_api_key},
            timeout=settings.job_search_timeout_seconds,
            # Every billable attempt is reserved/accounted by the orchestrator.
            retries=0, cache_seconds=0, respect_robots=False, allow_redirects=False,
        )
        if response.status_code != 200:
            code = "rate_limited" if response.status_code == 429 else "upstream"
            if response.status_code in (401, 403):
                code = "auth"
            raise SourceError("Search provider request failed", code=code)
        body = response.json()
        rows = body.get("web", {}).get("results", [])
        if not isinstance(rows, list):
            raise SourceError("Malformed search response", code="parse")
        return [SearchResult(row["url"][:2000], parse_datetime(row.get("page_age")))
                for row in rows[:request.count] if isinstance(row, dict) and isinstance(row.get("url"), str)]


# Add an adapter here, then select/reorder it with JOB_SEARCH_PROVIDERS.
PROVIDERS: dict[str, type[SearchProvider]] = {"brave": BraveSearchProvider}


def configured_providers() -> list[SearchProvider]:
    names = list(dict.fromkeys(n.strip().lower() for n in settings.job_search_providers.split(",") if n.strip()))
    if any(name not in PROVIDERS for name in names):
        raise ValueError("Unknown job search provider")
    if "brave" in names and not settings.brave_search_api_key:
        raise ValueError("Search credentials missing")
    return [PROVIDERS[name]() for name in names]
