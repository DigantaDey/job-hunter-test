"""
Shared outbound HTTP client.

Production rules enforced here:
* one pooled AsyncClient per process (connection reuse, bounded sockets);
* exponential backoff with jitter on 429/5xx, honouring ``Retry-After``;
* per-host politeness delay + concurrency cap (we never hammer a job board);
* optional robots.txt compliance before fetching HTML;
* response caching for idempotent GETs (discovery endpoints are polled).

Memory: this module lives for the whole worker process, and a discovery /
funding / company-intel sweep touches *thousands of distinct URLs and hosts*, so
nothing here may be an unbounded dict:

* ``_cache`` is a bounded LRU of :class:`CachedResponse` — the status code, the
  headers and the parsed body, never the raw ``httpx.Response`` (which pins the
  request, its stream, its elapsed timing and the client's cookie jar). Bodies
  over ``HTTP_CACHE_MAX_BODY_BYTES`` are served but not cached;
* ``_host_state`` (per-host politeness lock + last-call timestamp) is a bounded
  LRU too, and an entry whose lock a coroutine currently holds is never evicted
  — the politeness guarantee cannot be lost to make room for a cache entry;
* the SSRF guard's DNS cache is bounded in :mod:`app.services.net_guard`.

Counters for all three are on ``GET /api/ops/status``.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.lru import BoundedTTLMap
from app.core.metrics import inc, observe
from app.services.net_guard import OutboundURLBlocked, check_url, install_transport_guard

log = get_logger("app.http")

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_semaphore: Optional[asyncio.Semaphore] = None


@dataclass(frozen=True)
class CachedResponse:
    """
    What a cache hit actually needs to look like.

    Deliberately *not* an ``httpx.Response``: keeping one alive pins the request
    object, the decoded stream and the client's cookies for as long as the entry
    lives, which is exactly the leak this cache used to have. Consumers use
    ``status_code`` / ``headers`` / ``json()`` / ``text`` — and nothing else.
    """

    url: str
    status_code: int
    headers: httpx.Headers
    text: str
    json_body: Any = None
    is_cache_hit: bool = True

    def json(self) -> Any:
        if self.json_body is None:
            raise ValueError(f"cached response for {self.url} has no JSON body "
                             f"(content-type: {self.headers.get('content-type', 'unknown')})")
        return self.json_body

    def raise_for_status(self) -> "CachedResponse":
        return self

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def ok(self) -> bool:
        return self.is_success

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def __repr__(self) -> str:  # never log the body
        return f"<CachedResponse {self.status_code} {self.url} ({len(self.text)} chars, cached)>"


@dataclass
class _HostState:
    """Per-host politeness state: the serialising lock + the last call time."""

    lock: asyncio.Lock = None  # type: ignore[assignment]
    last_call: float = 0.0

    def __post_init__(self) -> None:
        if self.lock is None:
            self.lock = asyncio.Lock()


def _not_held(state: Any) -> bool:
    """A politeness lock a coroutine is holding must never be evicted."""
    lock = getattr(state, "lock", None)
    return not (lock is not None and lock.locked())


def _bounded(name: str, default_max: int, **kwargs) -> BoundedTTLMap:
    return BoundedTTLMap(name=name, max_entries=default_max, **kwargs)


#: GET-url -> CachedResponse. Hard-capped LRU; TTL comes from the caller's
#: ``cache_seconds`` at write time.
_cache: BoundedTTLMap = _bounded("http.cache", settings.http_cache_max_entries)

#: host -> _HostState. Hard-capped LRU that skips entries whose lock is held.
_host_state: BoundedTTLMap = _bounded("http.host_state", settings.http_host_state_max_entries,
                                      evictable=_not_held)


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                # The SSRF guard lives on the transport so that *every* hop —
                # including redirects httpx resolves internally — is checked.
                transport = install_transport_guard(
                    httpx.AsyncHTTPTransport(
                        limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
                        retries=0,
                    )
                )
                _client = httpx.AsyncClient(
                    transport=transport,
                    timeout=httpx.Timeout(settings.http_timeout, connect=min(10, settings.http_timeout)),
                    headers={
                        "User-Agent": settings.http_user_agent,
                        "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
                        "Accept-Language": "en",
                    },
                    follow_redirects=True,
                    max_redirects=5,
                )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_fetches))
    return _semaphore


def _host_state_for(host: str) -> _HostState:
    """
    Get-or-create the politeness state for *host* (no await between look and
    insert, so two coroutines cannot each create one).

    The lookup goes through the LRU's ``get`` so a host that is *actively* being
    fetched is the last candidate for eviction — not the one that happened to be
    inserted first.
    """
    state = _host_state.get(host)
    if state is None:
        state = _HostState()
        _host_state.put(host, state)
    return state


async def _polite_wait(host: str) -> None:
    """Serialise + space out requests to the same host."""
    state = _host_state_for(host)
    async with state.lock:
        gap = settings.per_host_min_interval_seconds - (time.monotonic() - state.last_call)
        if gap > 0:
            await asyncio.sleep(gap)
        state.last_call = time.monotonic()


def _cache_entry(response: httpx.Response, url: str) -> Optional[CachedResponse]:
    """Detach what consumers need from *response* (or None when it is too big to cache)."""
    body = response.text
    size = len(body.encode("utf-8", "replace"))
    if size > settings.http_cache_max_body_bytes:
        log.debug("http cache: not caching %s — %d bytes exceeds the %d byte cap",
                  url, size, settings.http_cache_max_body_bytes)
        return None
    json_body: Any = None
    if "json" in (response.headers.get("content-type") or "").lower():
        try:
            json_body = response.json()
        except ValueError:
            json_body = None  # advertised as JSON but unparseable: serve the text
    return CachedResponse(url=url, status_code=response.status_code,
                          headers=httpx.Headers(dict(response.headers)), text=body,
                          json_body=json_body)


def cache_stats() -> Dict[str, Any]:
    """Bounded-cache counters for ops/monitoring (never the cached bodies)."""
    return {**_cache.stats(), "max_body_bytes": settings.http_cache_max_body_bytes,
            "host_state": _host_state.stats()}


def clear_cache() -> None:
    _cache.clear()


async def request(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    retries: Optional[int] = None,
    cache_seconds: int = 0,
    allow_redirects: bool = True,
    respect_robots: Optional[bool] = None,
) -> Union[httpx.Response, CachedResponse]:
    """
    Perform an HTTP request with retries/backoff, policy checks and politeness controls.

    A cache hit returns a :class:`CachedResponse` (status + headers + parsed
    body) rather than the original ``httpx.Response``; both satisfy the surface
    callers use (``status_code``, ``headers``, ``json()``, ``text``).
    """
    host = urlparse(url).netloc
    cache_key = f"{method}:{url}:{sorted((params or {}).items())}"

    # Fail fast (and auditably) on URLs that must never leave the process.
    try:
        await check_url(url)
    except OutboundURLBlocked as exc:
        inc("jobhunter_http_blocked_total", reason="url_policy", host=host)
        log.warning("outbound request blocked: %s", exc.reason)
        raise
    if method.upper() == "GET" and cache_seconds > 0:
        cached = _cache.get(cache_key)
        if cached is not None:
            inc("jobhunter_http_cache_hits_total", host=host)
            log.debug("http cache hit for %s (%s)", url, cached)
            return cached

    should_check_robots = settings.respect_robots_txt if respect_robots is None else respect_robots
    if should_check_robots:
        from app.services.robots import can_fetch

        if not await can_fetch(url):
            inc("jobhunter_http_blocked_total", reason="robots_txt", host=host)
            raise PermissionError(f"robots.txt disallows fetching {url}")

    max_attempts = (settings.ai_max_retries if retries is None else retries) + 1
    client = await get_client()
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        async with _sem():
            await _polite_wait(host)
            started = time.perf_counter()
            try:
                response = await client.request(
                    method, url, params=params, json=json_body, headers=headers,
                    timeout=timeout or settings.http_timeout, follow_redirects=allow_redirects,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                inc("jobhunter_http_errors_total", host=host, kind=type(exc).__name__)
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(min(8.0, settings.ai_backoff_base ** attempt) * (0.7 + random.random() * 0.6))
                continue
            finally:
                observe("jobhunter_http_duration_seconds", time.perf_counter() - started, host=host)

        inc("jobhunter_http_requests_total", host=host, status=str(response.status_code))

        if response.status_code == 429 or 500 <= response.status_code < 600:
            if attempt >= max_attempts:
                return response
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() else settings.ai_backoff_base ** attempt
            log.warning("http %s %s -> %s, retrying in %.1fs", method, url, response.status_code, delay)
            await asyncio.sleep(min(20.0, delay))
            continue

        if method.upper() == "GET" and cache_seconds > 0 and response.status_code == 200:
            entry = _cache_entry(response, url)
            if entry is not None:
                _cache.put(cache_key, entry, ttl=cache_seconds)
        return response

    raise last_error or RuntimeError("request failed")


async def get_json(url: str, **kwargs) -> Any:
    response = await request("GET", url, **kwargs)
    if response.status_code != 200:
        raise httpx.HTTPStatusError(f"{response.status_code} from {url}", request=response.request, response=response)
    return response.json()


async def get_text(url: str, **kwargs) -> str:
    response = await request("GET", url, **kwargs)
    if response.status_code != 200:
        raise httpx.HTTPStatusError(f"{response.status_code} from {url}", request=response.request, response=response)
    return response.text
