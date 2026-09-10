"""Shared types + helpers for job source adapters."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


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

    def dedupe_key(self) -> str:
        if self.external_id:
            return f"{self.source}:{self.external_id}".lower()
        return f"{self.company}:{self.title}".strip().lower()[:280]

    def to_dict(self) -> Dict[str, Any]:
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
        }


class SourceError(RuntimeError):
    """Raised when a source cannot be used (missing key, gated, upstream down)."""


class Source:
    """Base class for adapters."""

    id: str = "base"
    label: str = ""
    kind: str = "api"  # api | board | rss | partner | gated
    requires_key: bool = False
    requires_account: bool = False
    enabled_by_default: bool = True

    def available(self) -> bool:  # pragma: no cover - overridden
        return True

    def unavailable_reason(self) -> str:  # pragma: no cover - overridden
        return "unavailable"

    def configured(self) -> bool:
        return True

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
