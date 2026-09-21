"""Source-level throttling.

The HTTP client already spaces requests *per host*. This limiter spaces them
*per adapter*, so two Recruitee boards on different subdomains still share a
budget, and a noisy source cannot starve the rest of a discovery fan-out.

The map is keyed by source id (a closed set of adapters) so it cannot grow
with the hosts a feed mentions.

Loop safety: an ``asyncio.Lock`` binds to an event loop on its *contended*
acquire path and refuses to run from any other loop. This throttle holds its
lock across an ``asyncio.sleep``, so contention is normal — a process that
runs a second loop (pytest-asyncio's per-test loops, a script doing repeated
``asyncio.run``) used to get ``RuntimeError: … is bound to a different event
loop`` on the first contended acquire there. The lock map, together with the
``_last``/``_window`` state (which is meaningless across loops), is rebuilt
for the running loop — the same pattern the HTTP client and the AI semaphore
use in ``app/services/http.py`` and ``app/services/ai_client.py``.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Dict, Optional

from app.services.sources.base import RateLimitPolicy, Source


class SourceThrottle:
    """Per-source token bucket + minimum interval."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        #: The loop the locks in ``_locks`` were created for; None = none yet.
        self._locks_loop: Optional[asyncio.AbstractEventLoop] = None
        self._last: Dict[str, float] = {}
        self._window: Dict[str, deque] = {}
        self.waits = 0
        self.acquires = 0

    def _lock(self, source_id: str) -> asyncio.Lock:
        # Only ever called from coroutines on the running loop, and there is
        # no await between the check and the rebuild, so a plain guard is
        # defensible — no threading.Lock (there is no second thread to race
        # against, and the map is touched from exactly one loop at a time).
        loop = asyncio.get_running_loop()
        if (self._locks_loop is None
                or self._locks_loop is not loop
                or self._locks_loop.is_closed()):
            # No recorded loop, or a different/closed one: the existing locks
            # may be bound to the old loop. Drop the whole map — and the
            # last-call stamps / rolling windows, which are meaningless across
            # loops — and rebuild for this one.
            self._locks = {}
            self._last.clear()
            self._window.clear()
            self._locks_loop = loop
        lock = self._locks.get(source_id)
        if lock is None:
            lock = self._locks[source_id] = asyncio.Lock()
        return lock

    async def acquire(self, source_id: str, policy: Optional[RateLimitPolicy] = None) -> float:
        """Block until this source may fire. Returns seconds waited."""
        policy = policy or RateLimitPolicy()
        rpm = max(1, int(policy.requests_per_minute or 60))
        interval = max(0.0, float(policy.min_interval_seconds or 0.0))
        waited = 0.0
        async with self._lock(source_id):
            now = time.monotonic()
            last = self._last.get(source_id, 0.0)
            gap = interval - (now - last)
            if gap > 0:
                self.waits += 1
                await asyncio.sleep(gap)
                waited += gap
                now = time.monotonic()
            stamps = self._window.setdefault(source_id, deque())
            while stamps and now - stamps[0] > 60.0:
                stamps.popleft()
            if len(stamps) >= rpm:
                delay = 60.0 - (now - stamps[0]) + 0.01
                if delay > 0:
                    self.waits += 1
                    await asyncio.sleep(delay)
                    waited += delay
                    now = time.monotonic()
                    while stamps and now - stamps[0] > 60.0:
                        stamps.popleft()
            stamps.append(now)
            self._last[source_id] = now
            self.acquires += 1
        return waited

    async def acquire_source(self, source: Source) -> float:
        return await self.acquire(source.id, source.rate_limit_policy)

    def stats(self) -> Dict[str, int]:
        return {"acquires": self.acquires, "waits": self.waits, "sources": len(self._locks)}

    def reset(self) -> None:
        # A "reset" that leaves the loop-bound locks behind does not reset:
        # the next acquire on a fresh loop would still hit the stale binding.
        self._last.clear()
        self._window.clear()
        self._locks.clear()
        self._locks_loop = None
        self.waits = 0
        self.acquires = 0


throttle = SourceThrottle()

__all__ = ["SourceThrottle", "throttle"]
