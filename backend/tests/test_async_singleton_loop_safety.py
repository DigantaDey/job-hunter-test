"""Async singletons must not be reused across event loops."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core.rate_limiter import rate_limiter
from app.services import ai_client, http
from app.services.sources.base import RateLimitPolicy
from app.services.sources.throttle import throttle as source_throttle


@pytest.fixture(autouse=True)
def _reset_singletons():
    yield
    http._client = None
    http._client_loop = None
    http._semaphore = None
    http._semaphore_loop = None
    ai_client._semaphore = None
    ai_client._semaphore_loop = None
    source_throttle.reset()


def _grab():
    async def _inner():
        return await http.get_client(), http._sem(), ai_client._sem()
    return asyncio.run(_inner())


def test_singletons_are_rebuilt_for_each_event_loop():
    first = _grab()
    second = _grab()
    assert first[0] is not second[0], "http client reused across a closed loop"
    assert first[1] is not second[1], "http semaphore reused across a closed loop"
    assert first[2] is not second[2], "ai semaphore reused across a closed loop"


def test_a_closed_loop_client_is_never_handed_out():
    stale = _grab()
    _grab()

    async def _check():
        client = await http.get_client()
        assert client is not stale[0]
        assert http._client_loop is asyncio.get_running_loop()
        assert not http._client_loop.is_closed()
    asyncio.run(_check())


def test_same_loop_keeps_the_same_client():
    async def _inner():
        return (await http.get_client()) is (await http.get_client())
    assert asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# The source throttle: loop-bound locks, contention is normal
# --------------------------------------------------------------------------- #
def _contended_acquire(source_id: str, policy: RateLimitPolicy):
    """Two tasks on the same source with a min interval, i.e. a contended
    acquire — the only path where an asyncio.Lock binds to its event loop.

    Returns the two acquires as a coroutine to run on a fresh asyncio.run.
    """
    async def contended():
        # The first acquire creates (or rebuilds) this loop's lock.
        await source_throttle.acquire(source_id, policy)
        # Seed _last so the in-lock sleep actually fires: its default 0.0 is
        # dwarfed by time.monotonic(), so the first contended pair would not
        # sleep and the lock would never bind.
        source_throttle._last[source_id] = time.monotonic()
        return await asyncio.gather(
            source_throttle.acquire(source_id, policy),
            source_throttle.acquire(source_id, policy),
        )
    return contended()


def test_source_throttle_survives_a_second_event_loop():
    """Contention in loop 1 binds its lock; loop 2 must not die on that
    binding — and the throttle must still actually wait, so a fix that
    silently disables throttling cannot pass this test."""
    policy = RateLimitPolicy(requests_per_minute=60, min_interval_seconds=0.3)
    waits1 = asyncio.run(_contended_acquire("loop-safety", policy))
    waits2 = asyncio.run(_contended_acquire("loop-safety", policy))
    assert max(waits1) > 0
    assert max(waits2) > 0


def test_source_throttle_reset_drops_stale_locks():
    """reset() is the documented escape hatch: it must also drop the
    loop-bound locks, or "reset" does not reset."""
    policy = RateLimitPolicy(requests_per_minute=60, min_interval_seconds=0.2)
    asyncio.run(_contended_acquire("reset-stale", policy))
    assert source_throttle._locks, "loop 1 should have created a lock"
    assert source_throttle._locks_loop is not None
    source_throttle.reset()
    assert source_throttle._locks == {}
    assert source_throttle._locks_loop is None
    # The next acquire on a fresh loop works — and still waits.
    waits = asyncio.run(_contended_acquire("reset-stale", policy))
    assert max(waits) > 0


def test_source_throttle_keeps_the_same_lock_within_one_loop():
    """The rebuild is per *loop*, not per call: within one loop the same lock
    is reused and requests to the same source are still spaced out."""
    async def inner():
        policy = RateLimitPolicy(requests_per_minute=60, min_interval_seconds=0.2)
        first_lock = source_throttle._lock("spaced")
        waited = [await source_throttle.acquire("spaced", policy) for _ in range(3)]
        second_lock = source_throttle._lock("spaced")
        return first_lock, second_lock, waited

    lock_a, lock_b, waited = asyncio.run(inner())
    assert lock_a is lock_b, "the fix must not rebuild the lock on every acquire"
    assert waited[0] == 0.0, "fresh state: nothing to wait for on the first acquire"
    assert waited[1] > 0.15, "the min interval is not enforced"
    assert waited[2] > 0.15, "the min interval is not enforced"


# --------------------------------------------------------------------------- #
# The AI rate limiter: same defect, lower reachability
# --------------------------------------------------------------------------- #
def test_rate_limiter_survives_a_second_loop_under_forced_contention():
    """The artificial contention is deliberate: acquire() never awaits while
    holding its lock, so a single loop can never reach the contended path —
    and the contended path is the only place an asyncio.Lock binds to a loop.
    Holding the lock from outside is the only way to bind it to loop 1 and
    prove loop 2 gets a working lock. Before the fix, the second asyncio.run
    raised RuntimeError: … is bound to a different event loop."""
    async def contested():
        async def holder():
            async with rate_limiter.lock:
                await asyncio.sleep(0.05)
        await asyncio.gather(holder(), rate_limiter.acquire(1))

    asyncio.run(contested())
    asyncio.run(contested())  # the regression: same shape, second event loop
