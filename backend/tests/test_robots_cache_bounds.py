"""
robots.txt cache bounds: the politeness cache is keyed on a netloc that arrives
from third-party job feeds — request-shaped, operator-uncontrolled data — so it
must be a hard-capped LRU like the SSRF guard's DNS verdict cache, not a dict
that only ever grows.

`robots._load` is monkeypatched everywhere: nothing in this file touches the
network.
"""
from __future__ import annotations

from urllib.robotparser import RobotFileParser

import pytest

from app.core.config import settings
from app.services import robots


@pytest.fixture(autouse=True)
def _clear_cache():
    robots.clear_cache()
    yield
    robots.clear_cache()


@pytest.fixture(autouse=True)
def _respect_robots(monkeypatch):
    # The robots gate must be on so can_fetch actually consults the cache.
    monkeypatch.setattr(settings, "respect_robots_txt", True, raising=False)


def _parser(lines=None) -> RobotFileParser:
    parser = RobotFileParser()
    parser.parse(lines or [])
    return parser


@pytest.mark.asyncio
async def test_robots_cache_stays_bounded(monkeypatch):
    """A sweep over more distinct feed hosts than the cap must not grow it forever."""

    async def _load(url: str) -> RobotFileParser:
        return _parser([])

    monkeypatch.setattr(robots, "_load", _load)
    for index in range(settings.robots_cache_max_entries + 200):
        assert await robots.can_fetch(f"https://sweep-{index}.example.com/job/1") is True

    stats = robots.cache_stats()
    assert len(robots._CACHE) <= stats["max_entries"] == settings.robots_cache_max_entries
    assert stats["evictions"] >= 200


@pytest.mark.asyncio
async def test_unreachable_robots_txt_is_cached_not_refetched(monkeypatch):
    """The sentinel regression: an unreachable robots.txt is "allow everything"
    and must be *remembered* as such — not read back as a miss, which would
    re-fetch the host on every request within the TTL."""
    calls = {"n": 0}

    async def _load(url: str) -> None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(robots, "_load", _load)
    url = "https://flaky.example.com/job/1"
    for _ in range(3):
        assert await robots.can_fetch(url) is True
    assert calls["n"] == 1
    # The read-only path sees the same thing: no parser, no fetch.
    assert robots.crawl_delay(url) is None
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_expired_robots_entry_is_refetched(monkeypatch):
    """The TTL is honoured on read: a lapsed entry is fetched again."""
    calls = {"n": 0}
    now = {"t": 1_000_000.0}

    async def _load(url: str) -> RobotFileParser:
        calls["n"] += 1
        return _parser([])

    monkeypatch.setattr(robots, "_load", _load)
    monkeypatch.setattr(robots._CACHE, "_clock", lambda: now["t"])

    url = "https://slow.example.com/job/1"
    assert await robots.can_fetch(url) is True
    assert calls["n"] == 1
    now["t"] += robots._TTL_SECONDS + 1  # past the TTL
    assert await robots.can_fetch(url) is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_crawl_delay_reads_the_cache_without_fetching(monkeypatch):
    """crawl_delay is a read-only path: it must never trigger a fetch."""
    calls = {"n": 0}

    async def _load(url: str) -> RobotFileParser:
        calls["n"] += 1
        return _parser(["User-agent: *", "Crawl-delay: 7"])

    monkeypatch.setattr(robots, "_load", _load)
    url = "https://polite.example.com/job/1"
    assert await robots.can_fetch(url) is True  # primes the cache
    assert calls["n"] == 1
    assert robots.crawl_delay(url) == 7.0
    assert calls["n"] == 1
    # An unvisited host is a cache miss, not a reason to fetch.
    assert robots.crawl_delay("https://unknown.example.com/job/1") is None
    assert calls["n"] == 1
