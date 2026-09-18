"""Source-level throttling.

The HTTP client already spaces requests *per host*. This limiter spaces them
*per adapter*, so two Recruitee boards on different subdomains still share a
budget, and a noisy source cannot starve the rest of a discovery fan-out.

The map is keyed by source id (a closed set of adapters) so it cannot grow
with the hosts a feed mentions.
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
        self._last: Dict[str, float] = {}
        self._window: Dict[str, deque] = {}
        self.waits = 0
        self.acquires = 0

    def _lock(self, source_id: str) -> asyncio.Lock:
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
        self._last.clear()
        self._window.clear()
        self.waits = 0
        self.acquires = 0


throttle = SourceThrottle()

__all__ = ["SourceThrottle", "throttle"]
