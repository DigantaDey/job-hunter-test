"""
Job source adapters.

Each adapter talks to a documented endpoint and returns ``Posting`` objects.
Everything is defensive: a board token that no longer exists, a renamed field or
an upstream 500 must never break discovery for the other sources.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from defusedxml import ElementTree

from app.core.config import settings
from app.core.logging import get_logger
from app.services import http as http_client
from app.services.sources.base import (
    Posting,
    RateLimitPolicy,
    Source,
    SourceError,
    compact_raw,
    keyword_score,
    parse_datetime,
    strip_html,
)

log = get_logger("app.sources.adapters")

# Well-known public ATS boards used as a starting point; users can extend the
# list per-account in Settings → Scraping (and via ATS_BOARD_TOKENS).
DEFAULT_BOARD_TOKENS: Dict[str, List[str]] = {
    "greenhouse": ["stripe", "databricks", "notion", "figma", "reddit", "coinbase",
                   "robinhood", "instacart", "doordash", "gitlab", "hashicorp", "samsara"],
    "lever": ["plaid", "brex", "kraken", "palantir", "eventbrite", "mixpanel"],
    "ashby": ["openai", "ramp", "linear", "cursor"],
    "workable": [],
    "recruitee": [],
    "smartrecruiters": ["Visa", "Ubisoft", "Bosch"],
    "personio": [],
    "workday": [],
}


def board_tokens_for(source_id: str, extra: Optional[List[str]] = None) -> List[str]:
    tokens: List[str] = []
    env_tokens = {
        "greenhouse": settings.greenhouse_board_tokens,
        "lever": settings.lever_board_tokens,
        "ashby": settings.ashby_board_tokens,
        "workable": settings.workable_board_tokens,
        "recruitee": settings.recruitee_board_tokens,
        "smartrecruiters": settings.smartrecruiters_board_tokens,
        "personio": settings.personio_board_tokens,
        "workday": settings.workday_board_tokens,
    }.get(source_id, "")
    env_list = env_tokens.split(",") if isinstance(env_tokens, str) else list(env_tokens or [])
    for group in (DEFAULT_BOARD_TOKENS.get(source_id, []), [t.strip() for t in env_list], extra or []):
        for token in group:
            token = (token or "").strip()
            if token and token not in tokens:
                tokens.append(token)
    return tokens


def _keywords_text(keywords: List[str]) -> str:
    return " ".join(k.strip() for k in (keywords or [])[:4] if k and k.strip())


def _relevant(posting: Posting, keywords: List[str]) -> bool:
    if not keywords:
        return True
    return keyword_score(f"{posting.title} {posting.description} {posting.location}", keywords) > 0


def _p(raw: Any, **kwargs) -> Posting:
    """Build a Posting and keep a compact copy of the source payload beside it."""
    if "raw" not in kwargs:
        kwargs["raw"] = compact_raw(raw if isinstance(raw, dict) else {"value": raw})
    return Posting(**kwargs)


# --------------------------------------------------------------------------- #
# Keyless public job APIs
# --------------------------------------------------------------------------- #
class RemotiveSource(Source):
    id, label, kind = "remotive", "Remotive (remote jobs)", "api"
    official_feed = True
    supports_keyword_search = True

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        params: Dict[str, Any] = {"limit": max(limit * 2, 20)}
        query = _keywords_text(keywords)
        if query:
            params["search"] = query
        data = await http_client.get_json("https://remotive.com/api/remote-jobs", params=params,
                                          cache_seconds=settings.discovery_cache_seconds)
        postings = []
        for raw in (data or {}).get("jobs", [])[: limit * 3]:
            posting = _p(raw,
                title=raw.get("title", ""),
                company=raw.get("company_name", ""),
                url=raw.get("url", ""),
                source=self.id,
                location=raw.get("candidate_required_location") or "Remote",
                description=strip_html(raw.get("description", "")),
                external_id=str(raw.get("id", "")),
                posted_at=parse_datetime(raw.get("publication_date")),
                salary=raw.get("salary", "") or "",
                remote=True,
                industry=raw.get("category", "") or "",
                extra={"job_type": raw.get("job_type", "")},
            )
            if posting.title and posting.company:
                postings.append(posting)
        return [p for p in postings if _relevant(p, keywords)][:limit]


class ArbeitnowSource(Source):
    id, label, kind = "arbeitnow", "Arbeitnow (EU/remote)", "api"
    official_feed = True
    supports_pagination = True
    supports_keyword_search = True

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        postings: List[Posting] = []
        for page in (1, 2):
            data = await http_client.get_json(
                "https://www.arbeitnow.com/api/job-board-api",
                params={"page": page},
                cache_seconds=settings.discovery_cache_seconds,
            )
            rows = (data or {}).get("data") or []
            if not rows:
                break
            for raw in rows:
                posting = _p(raw,
                    title=raw.get("title", ""),
                    company=raw.get("company_name", ""),
                    url=raw.get("url", ""),
                    source=self.id,
                    location=raw.get("location") or "Remote",
                    description=strip_html(raw.get("description", "")),
                    external_id=str(raw.get("slug", "")),
                    posted_at=parse_datetime(raw.get("created_at")),
                    remote=bool(raw.get("remote")),
                    industry=" ".join((raw.get("tags") or [])[:3]),
                    extra={"tags": (raw.get("tags") or [])[:8], "job_types": raw.get("job_types") or []},
                )
                if posting.title and posting.company:
                    postings.append(posting)
            if len(postings) >= limit * 2:
                break
        return [p for p in postings if _relevant(p, keywords)][:limit]


class JobicySource(Source):
    id, label, kind = "jobicy", "Jobicy (remote jobs)", "api"
    official_feed = True
    supports_keyword_search = True

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        params: Dict[str, Any] = {"count": min(50, max(20, limit * 2)), "geo": "anywhere"}
        tags = [k.strip().lower().replace(" ", "-") for k in (keywords or [])[:2] if k.strip()]
        if tags:
            params["tag"] = tags[0]
        data = await http_client.get_json("https://jobicy.com/api/v2/remote-jobs", params=params,
                                          cache_seconds=settings.discovery_cache_seconds)
        postings = []
        for raw in (data or {}).get("jobs", []):
            posting = _p(raw,
                title=raw.get("jobTitle", ""),
                company=raw.get("companyName", ""),
                url=raw.get("url", ""),
                source=self.id,
                location=raw.get("jobGeo") or "Remote",
                description=strip_html(raw.get("jobDescription") or raw.get("jobExcerpt") or ""),
                external_id=str(raw.get("id", "")),
                posted_at=parse_datetime(raw.get("pubDate")),
                remote=True,
                industry=raw.get("jobIndustry") if isinstance(raw.get("jobIndustry"), str) else "",
                extra={"level": raw.get("jobLevel", ""), "type": raw.get("jobType", "")},
            )
            if posting.title and posting.company:
                postings.append(posting)
        return [p for p in postings if _relevant(p, keywords)][:limit]


class RemoteOKSource(Source):
    id, label, kind = "remoteok", "RemoteOK", "api"
    official_feed = True
    supports_keyword_search = True

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        response = await http_client.request("GET", "https://remoteok.com/api",
                                             headers={"Accept": "application/json"},
                                             cache_seconds=settings.discovery_cache_seconds)
        if response.status_code != 200:
            raise SourceError(f"remoteok returned {response.status_code}",
                              code="upstream", status_code=response.status_code)
        rows = response.json()
        postings: List[Posting] = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict) or "legal" in raw or not raw.get("position"):
                continue
            salary = ""
            if raw.get("salary_min"):
                salary = f"{raw.get('salary_min')}-{raw.get('salary_max', '')} USD"
            posting = _p(raw,
                title=raw.get("position", ""),
                company=raw.get("company", ""),
                url=raw.get("url") or raw.get("apply_url") or "",
                source=self.id,
                location=raw.get("location") or "Remote",
                description=strip_html(raw.get("description", "")),
                external_id=str(raw.get("id") or raw.get("slug") or ""),
                posted_at=parse_datetime(raw.get("date") or raw.get("epoch")),
                salary=salary,
                remote=True,
                extra={"tags": (raw.get("tags") or [])[:8]},
            )
            if posting.title and posting.company:
                postings.append(posting)
        return [p for p in postings if _relevant(p, keywords)][:limit]


class HimalayasSource(Source):
    id, label, kind = "himalayas", "Himalayas (remote jobs)", "api"
    official_feed = True
    supports_keyword_search = True
    supports_pagination = True

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        data = await http_client.get_json(
            "https://himalayas.app/jobs/api",
            params={"limit": max(20, limit * 2), "offset": 0},
            cache_seconds=settings.discovery_cache_seconds,
        )
        rows = (data or {}).get("jobs") or (data if isinstance(data, list) else [])
        postings: List[Posting] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            url = raw.get("applicationLink") or raw.get("guid") or raw.get("url") or ""
            location = raw.get("locationRestrictions") or []
            posting = _p(raw,
                title=raw.get("title") or raw.get("jobTitle") or "",
                company=raw.get("companyName") or raw.get("company") or "",
                url=url,
                source=self.id,
                location=", ".join(location) if isinstance(location, list) and location else "Remote",
                description=strip_html(raw.get("description") or raw.get("excerpt") or ""),
                external_id=str(raw.get("guid") or raw.get("id") or ""),
                posted_at=parse_datetime(raw.get("pubDate") or raw.get("publishedDate")),
                salary=" ".join(str(raw.get(k)) for k in ("minSalary", "maxSalary") if raw.get(k)),
                remote=True,
                extra={"seniority": raw.get("seniority") or [], "categories": raw.get("categories") or []},
            )
            if posting.title and posting.company:
                postings.append(posting)
        return [p for p in postings if _relevant(p, keywords)][:limit]


class TheMuseSource(Source):
    id, label, kind = "themuse", "The Muse", "api"
    official_feed = True
    supports_pagination = True
    supports_keyword_search = True

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        postings: List[Posting] = []
        for page in (1, 2):
            data = await http_client.get_json(
                "https://www.themuse.com/api/public/jobs",
                params={"page": page, "descending": "true"},
                cache_seconds=settings.discovery_cache_seconds,
            )
            rows = (data or {}).get("results") or []
            if not rows:
                break
            for raw in rows:
                locations = raw.get("locations") or []
                posting = _p(raw,
                    title=raw.get("name", ""),
                    company=(raw.get("company") or {}).get("name", ""),
                    url=((raw.get("refs") or {}).get("landing_page")) or "",
                    source=self.id,
                    location=", ".join(loc.get("name", "") for loc in locations[:2]) or "Remote",
                    description=strip_html(raw.get("contents", "")),
                    external_id=str(raw.get("id", "")),
                    posted_at=parse_datetime(raw.get("publication_date")),
                    extra={"levels": [lvl.get("name") for lvl in (raw.get("levels") or [])][:3]},
                )
                if posting.title and posting.company:
                    postings.append(posting)
            if len(postings) >= limit * 2:
                break
        return [p for p in postings if _relevant(p, keywords)][:limit]


class WeWorkRemotelySource(Source):
    id, label, kind = "weworkremotely", "We Work Remotely (RSS)", "rss"
    official_feed = True
    supports_pagination = True

    FEEDS = (
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-design-jobs.rss",
    )

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        postings: List[Posting] = []
        for feed in self.FEEDS:
            try:
                text = await http_client.get_text(feed, cache_seconds=settings.discovery_cache_seconds)
            except Exception as exc:
                log.debug("weworkremotely feed failed: %s", exc)
                continue
            try:
                root = ElementTree.fromstring(text)
            except ElementTree.ParseError:
                continue
            for item in root.iter("item"):
                title_raw = (item.findtext("title") or "").strip()
                company, _, title = title_raw.partition(":")
                title = title.strip() or title_raw
                raw = {
                    "title": title_raw,
                    "link": (item.findtext("link") or "").strip(),
                    "guid": (item.findtext("guid") or "").strip(),
                    "pubDate": (item.findtext("pubDate") or "").strip(),
                    "region": (item.findtext("region") or "").strip(),
                }
                posting = _p(raw,
                    title=title,
                    company=company.strip() or "We Work Remotely",
                    url=raw["link"],
                    source=self.id,
                    location=(raw["region"] or "Remote"),
                    description=strip_html(item.findtext("description") or ""),
                    external_id=raw["guid"] or title,
                    posted_at=parse_datetime(raw["pubDate"]),
                    remote=True,
                )
                if posting.title:
                    postings.append(posting)
        return [p for p in postings if _relevant(p, keywords)][:limit]


# --------------------------------------------------------------------------- #
# Partner APIs (operator-supplied credentials)
# --------------------------------------------------------------------------- #
class AdzunaSource(Source):
    id, label, kind = "adzuna", "Adzuna (official partner API)", "partner"
    requires_key = True
    official_feed = True
    supports_keyword_search = True
    supports_pagination = True
    rate_limit_policy = RateLimitPolicy(requests_per_minute=20, min_interval_seconds=0.25)

    def available(self) -> bool:
        return bool(settings.adzuna_app_id and settings.adzuna_app_key)

    def configured(self) -> bool:
        return self.available()

    def unavailable_reason(self) -> str:
        return "set ADZUNA_APP_ID and ADZUNA_APP_KEY to enable this source"

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        query = _keywords_text(keywords) or "software engineer"
        url = f"https://api.adzuna.com/v1/api/jobs/{settings.adzuna_country}/search/1"
        data = await http_client.get_json(
            url,
            params={
                "app_id": settings.adzuna_app_id,
                "app_key": settings.adzuna_app_key,
                "results_per_page": min(50, max(10, limit)),
                "what": query,
                "content-type": "application/json",
                "max_days_old": max(1, since_hours // 24),
            },
            cache_seconds=settings.discovery_cache_seconds,
        )
        postings = []
        for raw in (data or {}).get("results", []):
            posting = _p(raw,
                title=raw.get("title", ""),
                company=(raw.get("company") or {}).get("display_name", ""),
                url=raw.get("redirect_url", ""),
                source=self.id,
                location=(raw.get("location") or {}).get("display_name", "") or "Remote",
                description=strip_html(raw.get("description", "")),
                external_id=str(raw.get("id", "")),
                posted_at=parse_datetime(raw.get("created")),
                salary=f"{raw.get('salary_min', '')}-{raw.get('salary_max', '')}".strip("-"),
                industry=(raw.get("category") or {}).get("label", ""),
            )
            if posting.title and posting.company:
                postings.append(posting)
        return postings[:limit]


class JoobleSource(Source):
    id, label, kind = "jooble", "Jooble (official API)", "partner"
    requires_key = True
    official_feed = True
    supports_keyword_search = True
    rate_limit_policy = RateLimitPolicy(requests_per_minute=20, min_interval_seconds=0.25)

    def available(self) -> bool:
        return bool(settings.jooble_api_key)

    def configured(self) -> bool:
        return self.available()

    def unavailable_reason(self) -> str:
        return "set JOOBLE_API_KEY to enable this source"

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        response = await http_client.request(
            "POST",
            f"https://jooble.org/api/{settings.jooble_api_key}",
            json_body={"keywords": _keywords_text(keywords) or "software engineer", "page": "1"},
            cache_seconds=settings.discovery_cache_seconds,
        )
        if response.status_code != 200:
            raise SourceError(f"jooble returned {response.status_code}",
                              code="upstream", status_code=response.status_code)
        postings = []
        for raw in (response.json() or {}).get("jobs", []):
            posting = _p(raw,
                title=raw.get("title", ""),
                company=raw.get("company", ""),
                url=raw.get("link", ""),
                source=self.id,
                location=raw.get("location") or "Remote",
                description=strip_html(raw.get("snippet", "")),
                external_id=str(raw.get("id", "")),
                posted_at=parse_datetime(raw.get("updated")),
                salary=raw.get("salary", "") or "",
            )
            if posting.title and posting.company:
                postings.append(posting)
        return postings[:limit]


class USAJobsSource(Source):
    id, label, kind = "usajobs", "USAJOBS (official API)", "partner"
    requires_key = True

    def available(self) -> bool:
        return bool(settings.usajobs_api_key and settings.usajobs_email)

    def configured(self) -> bool:
        return self.available()

    def unavailable_reason(self) -> str:
        return "set USAJOBS_API_KEY and USAJOBS_EMAIL to enable this source"

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        data = await http_client.get_json(
            "https://data.usajobs.gov/api/search",
            params={"Keyword": _keywords_text(keywords) or "software engineer",
                    "ResultsPerPage": min(50, max(10, limit))},
            headers={"Host": "data.usajobs.gov", "User-Agent": settings.usajobs_email,
                     "Authorization-Key": settings.usajobs_api_key},
            cache_seconds=settings.discovery_cache_seconds,
        )
        postings = []
        for item in ((data or {}).get("SearchResult") or {}).get("SearchResultItems", []):
            descriptor = item.get("MatchedObjectDescriptor") or {}
            details = ((descriptor.get("UserArea") or {}).get("Details") or {})
            posting = _p(descriptor,
                title=descriptor.get("PositionTitle", ""),
                company=descriptor.get("OrganizationName", ""),
                url=descriptor.get("PositionURI", ""),
                source=self.id,
                location=descriptor.get("PositionLocationDisplay", "") or "USA",
                description=strip_html(f"{details.get('JobSummary', '')} {details.get('MajorDuties', '')}"),
                external_id=str(descriptor.get("PositionID", "")),
                posted_at=parse_datetime(descriptor.get("PublicationStartDate")),
            )
            if posting.title:
                postings.append(posting)
        return postings[:limit]


# --------------------------------------------------------------------------- #
# ATS board APIs (per-company, public boards)
# --------------------------------------------------------------------------- #
class _BoardSource(Source):
    """Base for ATS board adapters: fans out over the configured board tokens."""

    kind = "board"
    official_feed = True
    token_key: str = ""
    rate_limit_policy = RateLimitPolicy(requests_per_minute=30, min_interval_seconds=0.2)

    async def boards(self, board_tokens: List[str]) -> List[str]:
        return board_tokens_for(self.id, board_tokens)[: settings.max_ats_boards_per_run]

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        tokens = await self.boards(board_tokens)
        if not tokens:
            return []
        per_board = max(5, limit // max(1, len(tokens)) + 5)
        results = await asyncio.gather(*(self.fetch_board(token, per_board) for token in tokens), return_exceptions=True)
        postings: List[Posting] = []
        for token, result in zip(tokens, results, strict=False):
            if isinstance(result, BaseException):
                log.debug("%s board %s failed: %s", self.id, token, result)
                continue
            postings.extend(result)
        return [p for p in postings if _relevant(p, keywords)][:limit]

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:  # pragma: no cover - overridden
        raise NotImplementedError


class GreenhouseSource(_BoardSource):
    id, label = "greenhouse", "Greenhouse boards"

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:
        data = await http_client.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
            params={"content": "true"},
            cache_seconds=settings.discovery_cache_seconds,
        )
        postings = []
        for raw in (data or {}).get("jobs", [])[:limit]:
            posting = _p(raw,
                title=raw.get("title", ""),
                company=(raw.get("company_name") or token).replace("-", " ").title(),
                url=raw.get("absolute_url", ""),
                source=self.id,
                location=(raw.get("location") or {}).get("name", "") or "Unspecified",
                description=strip_html(raw.get("content", "")),
                external_id=str(raw.get("id", "")),
                posted_at=parse_datetime(raw.get("updated_at") or raw.get("created_at")),
                extra={"board_token": token, "departments": [d.get("name") for d in (raw.get("departments") or [])][:3]},
            )
            if posting.title:
                postings.append(posting)
        return postings


class LeverSource(_BoardSource):
    id, label = "lever", "Lever boards"

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:
        data = await http_client.get_json(
            f"https://api.lever.co/v0/postings/{token}",
            params={"mode": "json"},
            cache_seconds=settings.discovery_cache_seconds,
        )
        postings = []
        for raw in (data if isinstance(data, list) else [])[:limit]:
            categories = raw.get("categories") or {}
            posting = _p(raw,
                title=raw.get("text", ""),
                company=token.replace("-", " ").title(),
                url=raw.get("hostedUrl", ""),
                source=self.id,
                location=categories.get("location") or "Unspecified",
                description=strip_html(raw.get("descriptionPlain") or raw.get("description") or ""),
                external_id=str(raw.get("id", "")),
                posted_at=parse_datetime(raw.get("createdAt")),
                remote="remote" in str(categories.get("location", "")).lower(),
                extra={"board_token": token, "team": categories.get("team", ""),
                       "commitment": categories.get("commitment", "")},
            )
            if posting.title:
                postings.append(posting)
        return postings


class AshbySource(_BoardSource):
    id, label = "ashby", "Ashby boards"

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:
        data = await http_client.get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{token}",
            params={"includeCompensation": "true"},
            cache_seconds=settings.discovery_cache_seconds,
        )
        postings = []
        for raw in (data or {}).get("jobs", [])[:limit]:
            if raw.get("isListed") is False:
                continue
            posting = _p(raw,
                title=raw.get("title", ""),
                company=token.replace("-", " ").title(),
                url=raw.get("jobUrl") or raw.get("applyUrl") or "",
                source=self.id,
                location=raw.get("location") or "Unspecified",
                description=strip_html(raw.get("descriptionHtml") or raw.get("descriptionPlainText") or ""),
                external_id=str(raw.get("id", "")),
                posted_at=parse_datetime(raw.get("publishedAt") or raw.get("updatedAt")),
                remote=bool(raw.get("isRemote")),
                extra={"board_token": token, "department": raw.get("department", ""),
                       "employment_type": raw.get("employmentType", "")},
            )
            if posting.title:
                postings.append(posting)
        return postings


class RecruiteeSource(_BoardSource):
    """Public Recruitee careers API: ``https://{company}.recruitee.com/api/offers/``."""

    id, label = "recruitee", "Recruitee boards"

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:
        data = await http_client.get_json(
            f"https://{token}.recruitee.com/api/offers/",
            cache_seconds=settings.discovery_cache_seconds,
        )
        postings = []
        for raw in (data or {}).get("offers", [])[:limit]:
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or raw.get("state") or "published").lower()
            location = raw.get("location") or ", ".join(filter(None, [
                raw.get("city"), raw.get("country_code") or raw.get("country"),
            ])) or "Unspecified"
            posting = _p(raw,
                title=raw.get("title", ""),
                company=(raw.get("company_name") or token).replace("-", " ").title(),
                url=raw.get("careers_url") or raw.get("url") or (
                    f"https://{token}.recruitee.com/o/{raw.get('slug') or raw.get('id')}"
                ),
                source=self.id,
                location=location,
                description=strip_html(raw.get("description") or raw.get("requirements") or ""),
                external_id=str(raw.get("id") or raw.get("slug") or ""),
                posted_at=parse_datetime(raw.get("published_at") or raw.get("created_at") or raw.get("updated_at")),
                remote=bool(raw.get("remote") or raw.get("remote_status")),
                extra={"board_token": token, "department": raw.get("department", ""),
                       "employment_type": raw.get("employment_type", ""), "status": status},
                expired=status in {"closed", "archived", "unlisted", "expired"},
            )
            if posting.title:
                postings.append(posting)
        return postings


class WorkableSource(_BoardSource):
    id, label = "workable", "Workable boards"

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:
        data = await http_client.get_json(
            f"https://apply.workable.com/api/v1/widget/accounts/{token}",
            params={"details": "true"},
            cache_seconds=settings.discovery_cache_seconds,
        )
        postings = []
        for raw in (data or {}).get("jobs", [])[:limit]:
            shortcode = raw.get("shortcode") or raw.get("code") or raw.get("id")
            posting = _p(raw,
                title=raw.get("title", ""),
                company=(data.get("name") or token).replace("-", " ").title(),
                url=raw.get("url") or f"https://apply.workable.com/{token}/j/{shortcode}/",
                source=self.id,
                location=", ".join(filter(None, [raw.get("city"), raw.get("country")])) or "Unspecified",
                description=strip_html(raw.get("description") or raw.get("requirements") or ""),
                external_id=str(shortcode or ""),
                posted_at=parse_datetime(raw.get("published_on") or raw.get("created_at")),
                remote=bool(raw.get("telecommuting")),
                extra={"board_token": token, "department": raw.get("department", "")},
            )
            if posting.title:
                postings.append(posting)
        return postings


class SmartRecruitersSource(_BoardSource):
    id, label = "smartrecruiters", "SmartRecruiters boards"

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:
        data = await http_client.get_json(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            params={"limit": limit},
            cache_seconds=settings.discovery_cache_seconds,
        )
        postings = []
        for raw in (data or {}).get("content", [])[:limit]:
            location = raw.get("location") or {}
            posting = _p(raw,
                title=raw.get("name", ""),
                company=(raw.get("company") or {}).get("name") or token,
                url=f"https://jobs.smartrecruiters.com/{token}/{raw.get('id')}",
                source=self.id,
                location=", ".join(filter(None, [location.get("city"), location.get("country")])) or "Unspecified",
                description=strip_html(raw.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", "")
                                       if isinstance(raw.get("jobAd"), dict) else ""),
                external_id=str(raw.get("id", "")),
                posted_at=parse_datetime(raw.get("releasedDate")),
                extra={"board_token": token, "department": (raw.get("department") or {}).get("label", "")},
            )
            if posting.title:
                postings.append(posting)
        return postings


class PersonioSource(_BoardSource):
    """Public Personio XML feed: ``https://{company}.jobs.personio.de/xml``.

    Tokens are the company subdomain. A ``company.de`` / ``company.com`` suffix
    pins the TLD; otherwise ``.de`` is tried first and ``.com`` is the fallback.
    """

    id, label = "personio", "Personio boards"

    def _hosts(self, token: str) -> List[str]:
        cleaned = (token or "").strip().lower()
        if cleaned.endswith(".de") or cleaned.endswith(".com"):
            host, _, tld = cleaned.rpartition(".")
            return [f"{host}.jobs.personio.{tld}"]
        return [f"{cleaned}.jobs.personio.de", f"{cleaned}.jobs.personio.com"]

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:
        last_error: Optional[BaseException] = None
        text = ""
        host_used = ""
        for host in self._hosts(token):
            try:
                text = await http_client.get_text(
                    f"https://{host}/xml",
                    params={"language": "en"},
                    cache_seconds=settings.discovery_cache_seconds,
                )
                host_used = host
                break
            except Exception as exc:  # noqa: BLE001 - try the next TLD
                last_error = exc
                continue
        if not text:
            raise SourceError(f"personio board {token} unavailable: {last_error}",
                              code="upstream") from last_error
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise SourceError(f"personio board {token} returned unparseable XML", code="parse") from exc
        postings: List[Posting] = []
        company = token.split(".")[0].replace("-", " ").title()
        for position in list(root.iter("position"))[:limit]:
            job_id = (position.findtext("id") or "").strip()
            title = (position.findtext("name") or "").strip()
            office = (position.findtext("office") or "").strip()
            descriptions = []
            for block in position.iter("jobDescription"):
                descriptions.append(block.findtext("value") or "")
            schedule = (position.findtext("schedule") or "").strip()
            employment = (position.findtext("employmentType") or "").strip()
            raw = {
                "id": job_id,
                "name": title,
                "office": office,
                "department": (position.findtext("department") or "").strip(),
                "employmentType": employment,
                "schedule": schedule,
                "createdAt": (position.findtext("createdAt") or "").strip(),
                "host": host_used,
            }
            posting = _p(raw,
                title=title,
                company=company,
                url=f"https://{host_used}/job/{job_id}" if job_id else f"https://{host_used}",
                source=self.id,
                location=office or "Unspecified",
                description=strip_html(" ".join(descriptions)),
                external_id=job_id or title,
                posted_at=parse_datetime(raw["createdAt"]),
                remote="remote" in f"{office} {title}".lower(),
                extra={"board_token": token, "department": raw["department"],
                       "employment_type": employment, "schedule": schedule},
            )
            if posting.title:
                postings.append(posting)
        return postings


class WorkdaySource(_BoardSource):
    """
    Workday CXS API. Tokens are ``host|tenant|site`` (e.g.
    ``acme.wd1.myworkdayjobs.com|acme|External``), configured explicitly because
    Workday has no global board index.
    """

    id, label = "workday", "Workday boards"

    async def boards(self, board_tokens: List[str]) -> List[str]:
        tokens = board_tokens_for(self.id, board_tokens)
        return [t for t in tokens if t.count("|") == 2][: settings.max_ats_boards_per_run]

    async def fetch_board(self, token: str, limit: int) -> List[Posting]:
        host, tenant, site = token.split("|")
        search_text = ""
        response = await http_client.request(
            "POST",
            f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
            json_body={"appliedFacets": {}, "limit": min(20, limit), "offset": 0, "searchText": search_text},
            headers={"Accept": "application/json"},
            cache_seconds=settings.discovery_cache_seconds,
        )
        if response.status_code != 200:
            raise SourceError(f"workday {host} returned {response.status_code}",
                              code="upstream", status_code=response.status_code)
        postings = []
        for raw in (response.json() or {}).get("jobPostings", [])[:limit]:
            path = raw.get("externalPath", "")
            posting = _p(raw,
                title=raw.get("title", ""),
                company=tenant.replace("-", " ").title(),
                url=f"https://{host}/en-US/{site}{path}" if path else f"https://{host}",
                source=self.id,
                location=raw.get("locationsText") or "Unspecified",
                description=strip_html(" ".join(str(b) for b in (raw.get("bulletFields") or []))),
                external_id=path or raw.get("title", ""),
                posted_at=parse_datetime(raw.get("postedOn")),
                extra={"board_token": token},
            )
            if posting.title:
                postings.append(posting)
        return postings


# --------------------------------------------------------------------------- #
# Sources that require partner access / an authenticated session.
# These are surfaced honestly in the UI instead of being faked.
# --------------------------------------------------------------------------- #
class _GatedSource(Source):
    kind = "gated"
    enabled_by_default = False
    requires_account = True
    reason = "unavailable"

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return self.reason

    async def fetch(self, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
        raise SourceError(self.reason, code="gated")


class LinkedInSource(_GatedSource):
    id, label = "linkedin", "LinkedIn"
    reason = ("requires LinkedIn partner/official API access or a licensed data provider — "
              "silent scraping violates the ToS and gets accounts banned")


class IndeedSource(_GatedSource):
    id, label = "indeed", "Indeed"
    reason = "Indeed job search APIs are partner-only; use a licensed aggregator instead"


class NaukriSource(_GatedSource):
    id, label = "naukri", "Naukri"
    reason = "requires a licensed Naukri/Info Edge data agreement"


class InstahyreSource(_GatedSource):
    id, label = "instahyre", "Instahyre"
    reason = "requires an authenticated employer/partner session"


ADAPTERS: Dict[str, Source] = {
    s.id: s
    for s in [
        RemotiveSource(), ArbeitnowSource(), JobicySource(), RemoteOKSource(), HimalayasSource(),
        TheMuseSource(), WeWorkRemotelySource(), AdzunaSource(), JoobleSource(), USAJobsSource(),
        GreenhouseSource(), LeverSource(), AshbySource(), WorkableSource(), RecruiteeSource(),
        SmartRecruitersSource(), PersonioSource(), WorkdaySource(),
        LinkedInSource(), IndeedSource(), NaukriSource(), InstahyreSource(),
    ]
}


def _source_meta(source: Source) -> Dict[str, Any]:
    caps = source.capabilities()
    return {
        "label": source.label,
        "kind": source.kind,
        "requires_key": source.requires_key,
        "requires_account": source.requires_account,
        "enabled_by_default": source.enabled_by_default,
        "configured": source.configured(),
        "adapter_version": source.adapter_version,
        "official_feed": caps.official_feed,
        "capabilities": caps.to_dict(),
        "rate_limit": source.rate_limit_policy.to_dict(),
        "retry": source.retry_policy.to_dict(),
        "freshness": source.freshness_policy.to_dict(),
    }


SOURCE_META: Dict[str, Dict[str, Any]] = {s.id: _source_meta(s) for s in ADAPTERS.values()}
