"""
Bounded in-process caches in the long-lived worker.

The worker sweeps thousands of *distinct* URLs and hosts per day (discovery,
funding, company intel), so every module-level cache it keeps used to be a slow
leak:

* ``http._cache`` stored whole ``httpx.Response`` objects (headers + decoded
  body + request + stream) keyed by GET-url, evicting an entry only when the
  *same* URL was requested again;
* ``http._host_locks`` / ``http._host_last_call`` grew one entry per unique host
  forever;
* ``net_guard``'s DNS verdict cache did the same.

All three are now bounded LRUs (:class:`app.core.lru.BoundedTTLMap`). This suite
pins the bounds, the cache-hit shape (status + headers + parsed body — never the
raw response) and the fact that politeness/backoff behaviour did not change.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

import httpx
import pytest

from app.core.config import settings
from app.core.lru import BoundedTTLMap
from app.services import http, net_guard


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeClient:
    """Stands in for ``httpx.AsyncClient`` and counts what the wire would see."""

    def __init__(self, responder=None):
        self.calls: List[str] = []
        self._responder = responder

    async def request(self, method, url, **kwargs):
        self.calls.append(f"{method} {url}")
        if self._responder is not None:
            return self._responder(len(self.calls), method, url)
        return httpx.Response(200, json={"n": len(self.calls), "url": str(url)},
                              headers={"content-type": "application/json"},
                              request=httpx.Request(method, url))


def _async(value):
    async def _get():
        return value

    return _get


@pytest.fixture()
def client(monkeypatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr(http, "get_client", _async(fake))
    return fake


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """
    Hermetic DNS + empty module singletons, so bounds are measured from zero.

    The client/semaphore/lock singletons belong to one event loop (as they do in
    a worker process); each test gets a fresh loop, so they are reset the way
    ``close_client()`` does at shutdown.
    """

    async def _public_resolver(*_args, **_kwargs):
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", _public_resolver)
    net_guard.clear_dns_cache()
    http.clear_cache()
    http._host_state.clear()
    http._semaphore = None
    http._semaphore_loop = None
    yield
    http.clear_cache()
    http._host_state.clear()
    http._semaphore = None
    http._semaphore_loop = None
    net_guard.clear_dns_cache()


async def _fetch(url: str, **kwargs) -> Any:
    kwargs.setdefault("respect_robots", False)
    kwargs.setdefault("cache_seconds", 60)
    return await http.request("GET", url, **kwargs)


# --------------------------------------------------------------------------- #
# The response cache cannot exceed its bound
# --------------------------------------------------------------------------- #
async def test_response_cache_stays_within_its_bound_under_many_distinct_urls(client):
    """One entry per distinct URL is exactly the shape that used to grow forever."""
    total = settings.http_cache_max_entries * 4

    for index in range(total):
        await _fetch(f"https://board-{index}.example.com/jobs/{index}")

    assert len(client.calls) == total
    assert len(http._cache) <= settings.http_cache_max_entries
    stats = http.cache_stats()
    assert stats["entries"] <= stats["max_entries"]
    assert stats["evictions"] >= total - settings.http_cache_max_entries, stats


async def test_the_cache_stores_a_detached_summary_not_the_raw_response(client):
    """No ``httpx.Response`` is pinned: status + headers + parsed body only."""
    await _fetch("https://boards.example.com/jobs/1")
    entry = http._cache.peek("GET:https://boards.example.com/jobs/1:[]")

    assert isinstance(entry, http.CachedResponse)
    assert not isinstance(entry, httpx.Response)
    assert entry.status_code == 200
    assert entry.json()["url"] == "https://boards.example.com/jobs/1"
    assert entry.headers["content-type"] == "application/json"
    assert not hasattr(entry, "stream")
    assert not hasattr(entry, "request")


async def test_a_cache_hit_is_served_without_touching_the_wire(client):
    first = await _fetch("https://boards.example.com/jobs/7")
    second = await _fetch("https://boards.example.com/jobs/7")

    assert len(client.calls) == 1
    assert getattr(second, "is_cache_hit", False) is True
    assert second.status_code == first.status_code == 200
    assert second.json() == first.json()
    assert second.text == first.text


async def test_a_cached_entry_expires_after_its_ttl(client, monkeypatch):
    clock = {"t": 1_000.0}
    monkeypatch.setattr(http._cache, "_clock", lambda: clock["t"])

    await _fetch("https://boards.example.com/jobs/8", cache_seconds=30)
    assert len(client.calls) == 1
    await _fetch("https://boards.example.com/jobs/8", cache_seconds=30)
    assert len(client.calls) == 1, "still fresh — must be served from the cache"

    clock["t"] += 31
    await _fetch("https://boards.example.com/jobs/8", cache_seconds=30)
    assert len(client.calls) == 2, "expired — must be refetched"


async def test_a_body_over_the_cap_is_served_but_not_cached(client, monkeypatch):
    """One huge feed must not evict (or dominate) the cache by itself."""
    monkeypatch.setattr(settings, "http_cache_max_body_bytes", 32, raising=False)

    response = await _fetch("https://boards.example.com/huge")
    assert response.status_code == 200
    assert len(http._cache) == 0
    assert len(client.calls) == 1


async def test_non_200_responses_are_not_cached(client, monkeypatch):
    monkeypatch.setattr(http, "get_client", _async(FakeClient(
        responder=lambda n, method, url: httpx.Response(
            404, json={"error": "nope"}, headers={"content-type": "application/json"},
            request=httpx.Request(method, url)))))

    response = await _fetch("https://boards.example.com/missing", retries=0)
    assert response.status_code == 404
    assert len(http._cache) == 0


async def test_clear_cache_empties_the_bound(client):
    await _fetch("https://boards.example.com/jobs/9")
    assert len(http._cache) == 1
    http.clear_cache()
    assert len(http._cache) == 0


async def test_a_json_body_that_is_not_json_is_still_served_as_text(client, monkeypatch):
    monkeypatch.setattr(http, "get_client", _async(FakeClient(
        responder=lambda n, method, url: httpx.Response(
            200, text="<html>not json</html>", headers={"content-type": "application/json"},
            request=httpx.Request(method, url)))))

    await _fetch("https://boards.example.com/lying")
    entry = http._cache.peek("GET:https://boards.example.com/lying:[]")
    assert entry.text == "<html>not json</html>"
    with pytest.raises(ValueError):
        entry.json()


# --------------------------------------------------------------------------- #
# Per-host politeness state is bounded — without losing the politeness guarantee
# --------------------------------------------------------------------------- #
async def test_host_state_is_bounded(client):
    total = settings.http_host_state_max_entries * 2
    for index in range(total):
        await _fetch(f"https://host-{index}.example.com/", cache_seconds=0)

    assert len(http._host_state) <= settings.http_host_state_max_entries
    assert http.cache_stats()["host_state"]["evictions"] > 0


async def test_a_held_politeness_lock_is_never_evicted(client):
    """
    Evicting the lock a coroutine is holding would let two requests to the same
    host run concurrently — so a pinned entry survives the sweep.
    """
    pinned = http._host_state_for("pinned.example.com")

    async with pinned.lock:
        for index in range(settings.http_host_state_max_entries + 50):
            http._host_state_for(f"sweep-{index}.example.com")
        assert "pinned.example.com" in http._host_state, "an in-use lock must not be evicted"
        size = len(http._host_state)

    assert size <= settings.http_host_state_max_entries + 1
    assert http._host_state_for("pinned.example.com") is pinned


async def test_an_actively_used_host_survives_a_sweep_of_others(client):
    """Eviction is LRU by *use*, so a busy host is not dropped for an idle one."""
    busy = http._host_state_for("busy.example.com")
    for index in range(settings.http_host_state_max_entries - 1):
        http._host_state_for(f"idle-{index}.example.com")

    # Touch the busy host, then sweep past the cap with brand-new hosts.
    assert http._host_state_for("busy.example.com") is busy
    for index in range(settings.http_host_state_max_entries + 10):
        http._host_state_for(f"sweep-{index}.example.com")
        http._host_state_for("busy.example.com")

    assert "busy.example.com" in http._host_state
    assert http._host_state_for("busy.example.com") is busy
    assert len(http._host_state) <= settings.http_host_state_max_entries


async def test_politeness_delay_is_unchanged(client, monkeypatch):
    """Two requests to one host are still spaced by ``per_host_min_interval_seconds``."""
    monkeypatch.setattr(settings, "per_host_min_interval_seconds", 0.25, raising=False)

    await _fetch("https://polite.example.com/a", cache_seconds=0)
    started = time.monotonic()
    await _fetch("https://polite.example.com/b", cache_seconds=0)
    elapsed = time.monotonic() - started

    assert len(client.calls) == 2
    assert elapsed >= 0.2, f"the second request did not wait: {elapsed:.3f}s"


async def test_requests_to_different_hosts_are_not_serialised(client, monkeypatch):
    monkeypatch.setattr(settings, "per_host_min_interval_seconds", 5.0, raising=False)

    started = time.monotonic()
    await _fetch("https://one.example.com/a", cache_seconds=0)
    await _fetch("https://two.example.com/b", cache_seconds=0)
    assert time.monotonic() - started < 1.0


async def test_concurrent_requests_to_one_host_are_serialised(client, monkeypatch):
    """The per-host lock still makes them run one after the other."""
    monkeypatch.setattr(settings, "per_host_min_interval_seconds", 0.15, raising=False)

    started = time.monotonic()
    await asyncio.gather(*(_fetch(f"https://serial.example.com/{i}", cache_seconds=0)
                           for i in range(3)))
    assert time.monotonic() - started >= 0.3
    assert len(client.calls) == 3


# --------------------------------------------------------------------------- #
# Backoff is unchanged
# --------------------------------------------------------------------------- #
async def test_retry_after_is_still_honoured_before_a_success(monkeypatch):
    def responder(count, method, url):
        status = 429 if count == 1 else 200
        headers = {"retry-after": "0"} if status == 429 else {"content-type": "application/json"}
        return httpx.Response(status, json={"n": count}, headers=headers,
                              request=httpx.Request(method, url))

    fake = FakeClient(responder=responder)
    monkeypatch.setattr(http, "get_client", _async(fake))
    response = await _fetch("https://boards.example.com/throttled", retries=2)

    assert len(fake.calls) == 2
    assert response.status_code == 200


async def test_exhausted_retries_return_the_last_error_response(monkeypatch):
    fake = FakeClient(responder=lambda n, method, url: httpx.Response(
        503, json={}, headers={"retry-after": "0"}, request=httpx.Request(method, url)))
    monkeypatch.setattr(http, "get_client", _async(fake))

    response = await _fetch("https://boards.example.com/down", retries=1)
    assert response.status_code == 503
    assert len(fake.calls) == 2
    assert len(http._cache) == 0


# --------------------------------------------------------------------------- #
# The LRU primitive itself
# --------------------------------------------------------------------------- #
def test_bounded_map_evicts_least_recently_used():
    cache: BoundedTTLMap = BoundedTTLMap(name="t", max_entries=3)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") == 1  # "a" is now the most recently used
    cache.put("d", 4)

    assert cache.keys() == ["c", "a", "d"]
    assert cache.peek("b") is None
    assert len(cache) == 3


def test_bounded_map_expires_entries_on_its_own_clock():
    now = {"t": 1_000.0}
    cache = BoundedTTLMap(name="t", max_entries=4, clock=lambda: now["t"])
    cache.put("a", 1, ttl=10)

    assert cache.get("a") == 1
    now["t"] += 11
    assert cache.get("a") is None
    assert len(cache) == 0


def test_bounded_map_reports_its_counters():
    cache = BoundedTTLMap(name="t", max_entries=1)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("b")
    cache.get("a")

    stats = cache.stats()
    assert stats["entries"] == 1
    assert stats["evictions"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_bounded_map_keeps_a_pinned_entry_rather_than_breaking_it():
    cache = BoundedTTLMap(name="t", max_entries=1, evictable=lambda value: value != "pinned")
    cache.put("a", "pinned")
    cache.put("b", "other")
    cache.put("c", "other")

    assert cache.peek("a") == "pinned"
    assert cache.stats()["over_capacity"] == 1


# --------------------------------------------------------------------------- #
# The SSRF guard's DNS cache is bounded too
# --------------------------------------------------------------------------- #
async def test_dns_cache_is_bounded():
    for index in range(settings.dns_cache_max_entries * 3):
        await net_guard.preflight(f"https://host-{index}.example.com/")

    assert len(net_guard._DNS_CACHE) <= settings.dns_cache_max_entries
    stats = net_guard.dns_cache_stats()
    assert stats["entries"] <= stats["max_entries"]
    assert stats["evictions"] > 0


async def test_dns_verdicts_are_still_cached_within_the_bound(monkeypatch):
    calls = {"n": 0}

    async def counting(*_args, **_kwargs):
        calls["n"] += 1
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", counting)
    await net_guard.preflight("https://cached.example.com/")
    await net_guard.preflight("https://cached.example.com/other")
    assert calls["n"] == 1


async def test_a_negative_dns_verdict_is_cached_too(monkeypatch):
    calls = {"n": 0}

    async def private(*_args, **_kwargs):
        calls["n"] += 1
        return ["10.9.8.7"]

    monkeypatch.setattr(net_guard, "_resolve", private)
    monkeypatch.setattr(settings, "outbound_allow_private", False, raising=False)
    for _ in range(3):
        with pytest.raises(net_guard.OutboundURLBlocked):
            await net_guard.check_url("https://internal-jobs.example.com/")
    assert calls["n"] == 1


def test_cache_stats_expose_the_bounds():
    payload: Dict[str, Any] = {"http_cache": http.cache_stats(),
                               "dns_cache": net_guard.dns_cache_stats()}
    assert payload["http_cache"]["max_entries"] == settings.http_cache_max_entries
    assert payload["http_cache"]["max_body_bytes"] == settings.http_cache_max_body_bytes
    assert payload["http_cache"]["host_state"]["max_entries"] == settings.http_host_state_max_entries
    assert payload["dns_cache"]["max_entries"] == settings.dns_cache_max_entries
    assert payload["dns_cache"]["ttl_seconds"] == 60.0
