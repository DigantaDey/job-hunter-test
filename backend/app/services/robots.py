"""
robots.txt client with an in-process cache.

Fetching job boards politely is a hard requirement for a production crawler, so
every outbound HTML/text fetch consults this module unless explicitly disabled
(``RESPECT_ROBOTS_TXT=false``, e.g. for a first-party ATS board the user owns).
"""
from __future__ import annotations

import time
from typing import Dict, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("app.robots")

_CACHE: Dict[str, tuple[float, Optional[RobotFileParser]]] = {}
_TTL_SECONDS = 3600


def _robots_url(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


async def _load(url: str) -> Optional[RobotFileParser]:
    import httpx

    robots_url = _robots_url(url)
    try:
        async with httpx.AsyncClient(timeout=6, headers={"User-Agent": settings.http_user_agent}) as client:
            response = await client.get(robots_url)
        parser = RobotFileParser()
        parser.set_url(robots_url)
        if response.status_code == 200:
            parser.parse(response.text.splitlines())
            return parser
        if response.status_code in (401, 403):
            parser.disallow_all = True  # type: ignore[attr-defined]  # CPython sets it; typeshed does not expose it
            return parser
        # 404 / 4xx → no restrictions
        parser.allow_all = True  # type: ignore[attr-defined]  # same runtime-only attribute
        return parser
    except Exception as exc:  # unreachable robots.txt must not block the app
        log.debug("robots.txt unavailable for %s: %s", robots_url, exc)
        return None


async def can_fetch(url: str, user_agent: str | None = None) -> bool:
    """True when the given user agent may fetch ``url``."""
    if not settings.respect_robots_txt:
        return True
    host = urlparse(url).netloc
    if not host:
        return True
    now = time.time()
    cached = _CACHE.get(host)
    if cached and now - cached[0] < _TTL_SECONDS:
        parser = cached[1]
    else:
        parser = await _load(url)
        _CACHE[host] = (now, parser)
    if parser is None:
        return True
    try:
        return parser.can_fetch(user_agent or settings.http_user_agent, url)
    except Exception:
        return True


def crawl_delay(url: str) -> Optional[float]:
    host = urlparse(url).netloc
    cached = _CACHE.get(host)
    if not cached or cached[1] is None:
        return None
    try:
        delay = cached[1].crawl_delay(settings.http_user_agent)
        return float(delay) if delay is not None else None
    except Exception:
        return None


def clear_cache() -> None:
    _CACHE.clear()
