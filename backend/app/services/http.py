"""
Shared outbound HTTP client.

Production rules enforced here:
* one pooled AsyncClient per process (connection reuse, bounded sockets);
* exponential backoff with jitter on 429/5xx, honouring ``Retry-After``;
* per-host politeness delay + concurrency cap (we never hammer a job board);
* optional robots.txt compliance before fetching HTML;
* response caching for idempotent GETs (discovery endpoints are polled).
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc, observe
from app.services.net_guard import OutboundURLBlocked, check_url, install_transport_guard

log = get_logger("app.http")

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_host_locks: Dict[str, asyncio.Lock] = {}
_host_last_call: Dict[str, float] = {}
_semaphore: Optional[asyncio.Semaphore] = None
_cache: Dict[str, Tuple[float, httpx.Response]] = {}


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


async def _polite_wait(host: str) -> None:
    """Serialise + space out requests to the same host."""
    lock = _host_locks.setdefault(host, asyncio.Lock())
    async with lock:
        last = _host_last_call.get(host, 0.0)
        gap = settings.per_host_min_interval_seconds - (time.monotonic() - last)
        if gap > 0:
            await asyncio.sleep(gap)
        _host_last_call[host] = time.monotonic()


def _cache_get(key: str, ttl: int) -> Optional[httpx.Response]:
    hit = _cache.get(key)
    if not hit:
        return None
    ts, response = hit
    if time.time() - ts > ttl:
        _cache.pop(key, None)
        return None
    return response


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
) -> httpx.Response:
    """Perform an HTTP request with retries/backoff, policy checks and politeness controls."""
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
        cached = _cache_get(cache_key, cache_seconds)
        if cached is not None:
            inc("jobhunter_http_cache_hits_total", host=host)
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
            _cache[cache_key] = (time.time(), response)
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
