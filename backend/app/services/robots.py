"""
robots.txt client with an in-process cache.

Fetching job boards politely is a hard requirement for a production crawler, so
every outbound HTML/text fetch consults this module unless explicitly disabled
(``RESPECT_ROBOTS_TXT=false``, e.g. for a first-party ATS board the user owns).

The cache is a hard-capped LRU (:class:`app.core.lru.BoundedTTLMap`) rather
than a dict that only ever grows: the key is ``urlparse(url).netloc``, and the
URLs are the posting URLs that arrive from third-party feeds — request-shaped,
operator-uncontrolled data. The repo's invariant (``app/core/middleware.py``:
"nothing keyed on request data may be unbounded") applies exactly here, and the
DNS verdict cache in ``app/services/net_guard.py`` was bounded for the same
reason. Counters (entries/evictions/hits) are on ``GET /api/ops/status`` under
``outbound.robots_cache``.

The sentinel below exists because this cache deliberately stores ``None`` to
mean "robots.txt unreachable, so allow everything". ``BoundedTTLMap.get()``
returns its default for an *absent* key, so a stored ``None`` would be
indistinguishable from a miss and every unreachable host would be re-fetched on
every request — a memory fix turned into request amplification. Unreachable is
therefore stored as ``_UNREACHABLE`` and translated back at the read sites.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from app.core.config import settings
from app.core.logging import get_logger
from app.core.lru import BoundedTTLMap

log = get_logger("app.robots")

_TTL_SECONDS = 3600

#: Stored instead of ``None`` when robots.txt could not be fetched: "unreachable,
#: allow everything". A plain ``None`` value is indistinguishable from an absent
#: key in ``BoundedTTLMap`` (see the module docstring), which would turn this
#: cache into a re-fetch-every-request amplifier for unreachable hosts.
_UNREACHABLE = object()

#: host -> RobotFileParser | _UNREACHABLE. Bounded LRU + TTL: a host never
#: revisited used to stay here (with its parsed parser) for the whole process
#: lifetime, and the key set is not bounded by anything.
_CACHE = BoundedTTLMap(
    name="robots",
    max_entries=settings.robots_cache_max_entries,
    default_ttl=_TTL_SECONDS,
)


def _robots_url(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


async def _load(url: str) -> Optional[RobotFileParser]:
    robots_url = _robots_url(url)
    try:
        from app.services.http import get_client

        # Reuse shared pooled client (SSRF guard, connection reuse) — per-request
        # timeout keeps the robots fetch bounded. This replaces the previous
        # per-call AsyncClient which leaked connections and bypassed the guard.
        http_client = await get_client()
        response = await http_client.get(robots_url, timeout=6, headers={"User-Agent": settings.http_user_agent})
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


def _stored_host(parser: Optional[RobotFileParser]) -> object:
    """What goes into the cache for *parser*: the sentinel, never a bare None."""
    return parser if parser is not None else _UNREACHABLE


def _live_parser(stored: object) -> Optional[RobotFileParser]:
    """What a cached entry means at a read site: the parser, or None when the
    host was unreachable (which means "allow everything", not "re-fetch")."""
    return None if stored is _UNREACHABLE else stored  # type: ignore[return-value]


async def can_fetch(url: str, user_agent: str | None = None, *, force: bool = False) -> bool:
    """True when the given user agent may fetch ``url``."""
    if not force and not settings.respect_robots_txt:
        return True
    host = urlparse(url).netloc
    if not host:
        return True
    # ``__contains__`` honours the TTL (and is correct for a stored sentinel),
    # so an expired or absent entry is re-fetched; a live entry is used.
    if host in _CACHE:
        parser = _live_parser(_CACHE.get(host))
    else:
        parser = await _load(url)
        _CACHE.put(host, _stored_host(parser))
    if parser is None:
        return True
    try:
        return parser.can_fetch(user_agent or settings.http_user_agent, url)
    except Exception:
        return True


def crawl_delay(url: str) -> Optional[float]:
    host = urlparse(url).netloc
    if not host or host not in _CACHE:
        return None
    parser = _live_parser(_CACHE.get(host))
    if parser is None:
        return None
    try:
        delay = parser.crawl_delay(settings.http_user_agent)
        return float(delay) if delay is not None else None
    except Exception:
        return None


def cache_stats() -> dict:
    """Bounded-cache counters for ``/api/ops/status`` (never the parsers)."""
    return _CACHE.stats()


def clear_cache() -> None:
    _CACHE.clear()
