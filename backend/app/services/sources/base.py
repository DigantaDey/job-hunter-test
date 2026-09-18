"""Shared types + helpers for job source adapters.

The rest of the app talks to sources only through this module:

* identification (``id`` / ``label`` / ``kind`` / ``adapter_version``)
* capability declaration (``SourceCapabilities``)
* fetching + pagination (``fetch`` / ``fetch_page``)
* rate limits (``RateLimitPolicy``)
* retry behaviour (``RetryPolicy``)
* freshness (``FreshnessPolicy``)
* canonicalization (``Posting.canonical_id`` / ``content_hash``)
* error classification (``SourceError.code``)
* source health (recorded by the registry, not by adapters)

``Posting`` is the only shape adapters emit. Persistence, scoring and the UI
never see a raw Greenhouse/Lever/… payload — that lives on
``Posting.raw`` and is stored in ``jobs.raw_payload``, separate from the
normalized row and from ``jobs.extra``.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

#: Adapter protocol version shipped with this release. Bumped when the
#: normalized posting shape or a public feed contract changes.
ADAPTER_VERSION = "1.8.0"

#: Closed vocabulary for ``SourceError.code`` / health last-error.
ERROR_CODES: Tuple[str, ...] = (
    "unavailable",
    "gated",
    "auth",
    "rate_limited",
    "timeout",
    "upstream",
    "not_found",
    "parse",
    "robots",
    "unknown",
)

RETRYABLE_CODES = frozenset({"rate_limited", "timeout", "upstream"})


def strip_html(value: str, limit: int = 4000) -> str:
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()[:limit]


def parse_datetime(value: Any) -> Optional[datetime]:
    """Best-effort timestamp parsing (ISO-8601, epoch seconds, RFC-2822)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 1e12:  # milliseconds
                seconds /= 1000.0
            return datetime.utcfromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_datetime(int(text))
    cleaned = text.replace("Z", "+00:00")
    for parser in (
        lambda t: datetime.fromisoformat(t),
        lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
        lambda t: datetime.strptime(t, "%a, %d %b %Y %H:%M:%S %z"),
        lambda t: datetime.strptime(t, "%d %b %Y"),
    ):
        try:
            parsed = parser(cleaned)
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except (ValueError, TypeError):
            continue
    return None


def compact_raw(raw: Any, *, limit: int = 4000) -> Dict[str, Any]:
    """A bounded, HTML-stripped copy of a source payload.

    Stored separately from the normalized posting so a re-parse is possible
    without keeping multi-kilobyte job descriptions in ``jobs.extra``.
    """
    html_keys = {
        "content", "description", "descriptionhtml", "descriptionplaintext",
        "descriptionplain", "jobdescription", "jobexcerpt", "contents",
        "requirements", "snippet", "jobad",
    }
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {"_value": str(raw)[:limit]}

    out: Dict[str, Any] = {}
    budget = limit
    for key, value in list(raw.items())[:40]:
        if budget <= 0:
            break
        lowered = str(key).lower()
        if lowered in html_keys:
            continue
        if isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
            budget -= 16
        elif isinstance(value, str):
            out[key] = value[: min(400, budget)]
            budget -= len(out[key])
        elif isinstance(value, dict):
            nested = compact_raw(value, limit=min(800, budget))
            if nested:
                out[key] = nested
                budget -= 80
        elif isinstance(value, list):
            out[key] = value[:8]
            budget -= 40
    return out


def posting_content_hash(title: str, company: str, description: str) -> str:
    """Cross-source duplicate signal — not part of the unique constraint."""
    payload = f"{(title or '').strip().lower()}\n{(company or '').strip().lower()}\n{(description or '')[:4000]}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True)
class SourceCapabilities:
    """What a source can actually do — declared, never inferred from a URL."""

    fetch: bool = True
    pagination: bool = False
    keyword_search: bool = False
    board_tokens: bool = False
    official_feed: bool = False
    browser_assisted: bool = False
    requires_key: bool = False
    requires_account: bool = False
    gated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RateLimitPolicy:
    """Source-level throttle (on top of the HTTP client's per-host politeness)."""

    requests_per_minute: int = 60
    min_interval_seconds: float = 0.0
    burst: int = 4

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    backoff_base: float = 1.5
    retry_on: Tuple[str, ...] = ("timeout", "rate_limited", "upstream")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_base": self.backoff_base,
            "retry_on": list(self.retry_on),
        }


@dataclass(frozen=True)
class FreshnessPolicy:
    """How this source talks about time.

    ``honors_posted_at``: a missing timestamp means "freshness unknown" (inside
    the window), never "posted now". ``expire_after_hours`` is an optional
    source-side TTL; ``None`` means the source does not auto-expire listings.
    """

    default_window_hours: int = 24 * 7
    honors_posted_at: bool = True
    expire_after_hours: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FetchRequest:
    """One page of a source fetch. Adapters that ignore pagination still work."""

    keywords: List[str] = field(default_factory=list)
    limit: int = 15
    since_hours: int = 24 * 7
    board_tokens: List[str] = field(default_factory=list)
    cursor: Optional[str] = None
    page: int = 1


@dataclass
class FetchPage:
    items: List["Posting"] = field(default_factory=list)
    cursor: Optional[str] = None
    has_more: bool = False
    page: int = 1


@dataclass
class SourceHealth:
    """In-process snapshot of how a source has been behaving."""

    source_id: str
    status: str = "unknown"  # healthy | degraded | down | gated | unconfigured | unknown
    last_success_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    consecutive_failures: int = 0
    last_error_code: Optional[str] = None
    last_error: Optional[str] = None
    latency_ms: Optional[float] = None
    results_last_fetch: int = 0
    fetches: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
            "consecutive_failures": self.consecutive_failures,
            "last_error_code": self.last_error_code,
            "last_error": self.last_error,
            "latency_ms": self.latency_ms,
            "results_last_fetch": self.results_last_fetch,
            "fetches": self.fetches,
        }


@dataclass
class Posting:
    """Normalised job posting — the only shape the rest of the app sees."""

    title: str
    company: str
    url: str
    source: str
    location: str = "Remote"
    description: str = ""
    external_id: str = ""
    posted_at: Optional[datetime] = None
    salary: str = ""
    remote: bool = False
    industry: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    #: Compact original payload; persisted in ``jobs.raw_payload``, never
    #: mixed into the normalized ``jobs`` columns or ``jobs.extra``.
    raw: Dict[str, Any] = field(default_factory=dict)
    source_kind: str = ""
    adapter_version: str = ADAPTER_VERSION
    fetched_at: Optional[datetime] = None
    expired: bool = False
    expires_at: Optional[datetime] = None
    content_hash: str = ""
    title_normalized: str = ""
    company_name_normalized: str = ""

    def dedupe_key(self) -> str:
        """Canonical job identity for this source.

        ``lower("{source}:{external_id}")`` when the source has a stable id,
        else ``lower("{company}:{title}")``. Max 280 chars. This is the
        identity used to merge duplicates — never a URL, never a content hash.
        """
        if self.external_id:
            return f"{self.source}:{self.external_id}".lower()[:280]
        company = (self.company_name_normalized or self.company).strip().lower()
        title = (self.title_normalized or self.title).strip().lower()
        return f"{company}:{title}".strip()[:280]

    def canonical_id(self) -> str:
        return self.dedupe_key()

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        moment = now or datetime.utcnow()
        if self.expired:
            return True
        if self.expires_at is not None and self.expires_at <= moment:
            return True
        status = str((self.extra or {}).get("status") or "").lower()
        if status in {"closed", "expired", "unlisted", "archived"}:
            return True
        return False

    def ensure_normalized(self) -> "Posting":
        """Fill derived identity fields if the adapter left them blank."""
        if not self.title_normalized:
            self.title_normalized = (self.title or "").strip().lower()[:300]
        if not self.company_name_normalized:
            try:
                from app.services.company_normalize import normalize_company_name

                self.company_name_normalized = normalize_company_name(self.company)
            except Exception:  # pragma: no cover - defensive
                self.company_name_normalized = (self.company or "").strip().lower()[:200]
        if not self.content_hash:
            self.content_hash = posting_content_hash(
                self.title_normalized, self.company_name_normalized, self.description or ""
            )
        if self.fetched_at is None:
            self.fetched_at = datetime.utcnow()
        if not self.adapter_version:
            self.adapter_version = ADAPTER_VERSION
        return self

    def to_dict(self) -> Dict[str, Any]:
        self.ensure_normalized()
        return {
            "title": self.title.strip()[:300],
            "company": self.company.strip()[:200],
            "location": (self.location or "Remote").strip()[:200],
            "description": (self.description or "").strip()[:6000],
            "url": (self.url or "").strip()[:1000],
            "source": self.source,
            "external_id": self.external_id[:200],
            "posted_at": self.posted_at,
            "salary": self.salary[:200],
            "remote": bool(self.remote),
            "industry": self.industry,
            "extra": self.extra,
            # Additive identity / lifecycle fields. Existing readers ignore them.
            "dedupe_key": self.dedupe_key(),
            "canonical_id": self.canonical_id(),
            "content_hash": self.content_hash,
            "source_kind": self.source_kind,
            "adapter_version": self.adapter_version,
            "fetched_at": self.fetched_at,
            "expired": self.is_expired(),
            "expires_at": self.expires_at,
            "title_normalized": self.title_normalized,
            "company_name_normalized": self.company_name_normalized,
            # Raw payload travels next to the dict so discovery can persist it
            # in ``jobs.raw_payload`` and keep it out of ``jobs.extra``.
            "raw": self.raw or {},
        }


class SourceError(RuntimeError):
    """Raised when a source cannot be used (missing key, gated, upstream down).

    ``code`` is from :data:`ERROR_CODES`. ``retryable`` is the classified
    answer to "should the registry try again?", not a guess from the message.
    ``str(exc)`` stays the human message so existing report assertions
    (``errors[source] == "upstream down"``) keep working.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "unknown",
        retryable: Optional[bool] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "unknown"
        self.retryable = bool(RETRYABLE_CODES.__contains__(self.code) if retryable is None else retryable)
        self.status_code = status_code


def classify_error(exc: BaseException) -> SourceError:
    """Map an arbitrary failure onto the closed error vocabulary."""
    if isinstance(exc, SourceError):
        return exc
    if isinstance(exc, (TimeoutError,)):
        return SourceError("timeout", code="timeout", retryable=True)
    try:
        import asyncio

        if isinstance(exc, asyncio.TimeoutError):
            return SourceError("timeout", code="timeout", retryable=True)
    except Exception:  # pragma: no cover
        pass
    if isinstance(exc, PermissionError):
        return SourceError(str(exc) or "robots.txt disallows fetching", code="robots", retryable=False)
    name = type(exc).__name__
    message = str(exc) or name
    lowered = message.lower()
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status == 429 or "rate" in lowered and "limit" in lowered:
        return SourceError(message, code="rate_limited", retryable=True, status_code=status)
    if status in {401, 403} or "unauthorized" in lowered or "forbidden" in lowered:
        return SourceError(message, code="auth", retryable=False, status_code=status)
    if status == 404:
        return SourceError(message, code="not_found", retryable=False, status_code=status)
    if status is not None and 500 <= status < 600:
        return SourceError(message, code="upstream", retryable=True, status_code=status)
    if "timeout" in lowered or name in {"ConnectTimeout", "ReadTimeout", "TimeoutException"}:
        return SourceError(message, code="timeout", retryable=True, status_code=status)
    if name in {"HTTPStatusError", "HTTPError", "ConnectError", "NetworkError"}:
        return SourceError(message, code="upstream", retryable=True, status_code=status)
    if "parse" in lowered or name in {"JSONDecodeError", "ParseError"}:
        return SourceError(message, code="parse", retryable=False, status_code=status)
    return SourceError(f"{name}: {message}" if name not in message else message, code="unknown", retryable=False)


class Source:
    """Stable adapter interface. Subclasses override ``fetch`` (and optionally
    ``fetch_page``); everything else has a working default."""

    id: str = "base"
    label: str = ""
    kind: str = "api"  # api | board | rss | partner | gated | browser
    adapter_version: str = ADAPTER_VERSION
    requires_key: bool = False
    requires_account: bool = False
    enabled_by_default: bool = True
    #: Official/public ATS or job-board feed (preferred over browser scraping).
    official_feed: bool = False
    supports_pagination: bool = False
    supports_keyword_search: bool = False
    browser_assisted: bool = False
    rate_limit_policy: RateLimitPolicy = RateLimitPolicy()
    retry_policy: RetryPolicy = RetryPolicy()
    freshness_policy: FreshnessPolicy = FreshnessPolicy()

    def identity(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "adapter_version": self.adapter_version,
        }

    def capabilities(self) -> SourceCapabilities:
        gated = self.kind == "gated" or self.requires_account
        return SourceCapabilities(
            fetch=not gated,
            pagination=bool(self.supports_pagination),
            keyword_search=bool(self.supports_keyword_search or self.kind in {"api", "partner", "rss"}),
            board_tokens=self.kind == "board",
            official_feed=bool(self.official_feed or self.kind in {"board", "api", "rss", "partner"}),
            browser_assisted=bool(self.browser_assisted),
            requires_key=bool(self.requires_key),
            requires_account=bool(self.requires_account),
            gated=gated,
        )

    def available(self) -> bool:  # pragma: no cover - overridden
        return True

    def unavailable_reason(self) -> str:  # pragma: no cover - overridden
        return "unavailable"

    def configured(self) -> bool:
        return True

    def canonicalize(self, posting: Posting) -> Posting:
        """Fill identity / freshness fields the adapter left blank."""
        if not posting.source:
            posting.source = self.id
        if not posting.source_kind:
            posting.source_kind = self.kind
        if not posting.adapter_version:
            posting.adapter_version = self.adapter_version
        return posting.ensure_normalized()

    def classify_error(self, exc: BaseException) -> SourceError:
        return classify_error(exc)

    async def fetch_page(self, request: FetchRequest) -> FetchPage:
        """One page. Default: call ``fetch`` and report no further pages."""
        items = await self.fetch(
            keywords=list(request.keywords or []),
            limit=int(request.limit or 15),
            since_hours=int(request.since_hours or 24 * 7),
            board_tokens=list(request.board_tokens or []),
        )
        return FetchPage(items=list(items or []), cursor=None, has_more=False, page=max(1, request.page))

    async def fetch(
        self,
        *,
        keywords: List[str],
        limit: int,
        since_hours: int,
        board_tokens: List[str],
    ) -> List[Posting]:  # pragma: no cover - overridden
        raise NotImplementedError


def keyword_score(text: str, keywords: List[str]) -> int:
    """Simple relevance score used to filter/rank keyword-driven results."""
    if not keywords:
        return 1
    hay = (text or "").lower()
    return sum(1 for kw in keywords if kw and kw.lower() in hay)


def merge_postings(existing: Posting, incoming: Posting) -> Posting:
    """Idempotent merge of two listings of the same canonical job.

    Keeps the richer description, the non-empty salary, the remote flag if
    either side says so, and the newest ``posted_at``. Never invents a
    timestamp. The first-seen identity (source + external_id) of ``existing``
    wins so a later alias cannot retarget the row.
    """
    existing.ensure_normalized()
    incoming.ensure_normalized()
    if incoming.expired or incoming.is_expired():
        existing.expired = True
        existing.expires_at = existing.expires_at or incoming.expires_at or datetime.utcnow()
    if len(incoming.description or "") > len(existing.description or ""):
        existing.description = incoming.description
    if incoming.salary and not existing.salary:
        existing.salary = incoming.salary
    if incoming.remote:
        existing.remote = True
    if incoming.url and not existing.url:
        existing.url = incoming.url
    if incoming.location and (not existing.location or existing.location == "Remote"):
        existing.location = incoming.location
    if incoming.posted_at and (existing.posted_at is None or incoming.posted_at > existing.posted_at):
        existing.posted_at = incoming.posted_at
    extra = dict(existing.extra or {})
    extra.update({k: v for k, v in (incoming.extra or {}).items() if v not in (None, "", [], {})})
    existing.extra = extra
    if incoming.raw:
        existing.raw = dict(incoming.raw)
    existing.content_hash = posting_content_hash(
        existing.title_normalized, existing.company_name_normalized, existing.description or ""
    )
    existing.fetched_at = incoming.fetched_at or existing.fetched_at or datetime.utcnow()
    return existing


__all__ = [
    "ADAPTER_VERSION",
    "ERROR_CODES",
    "RETRYABLE_CODES",
    "FetchPage",
    "FetchRequest",
    "FreshnessPolicy",
    "Posting",
    "RateLimitPolicy",
    "RetryPolicy",
    "Source",
    "SourceCapabilities",
    "SourceError",
    "SourceHealth",
    "classify_error",
    "compact_raw",
    "keyword_score",
    "merge_postings",
    "parse_datetime",
    "posting_content_hash",
    "strip_html",
]
