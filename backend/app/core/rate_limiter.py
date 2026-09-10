import asyncio
import time
from collections import deque


class TokenBucketRateLimiter:
    """
    Token bucket for AI RPM.
    Thread-safe for asyncio.
    Ensures we never exceed rpm within a rolling 60s window.
    """
    def __init__(self, rpm: int = 60):
        self.rpm = rpm
        self.tokens = rpm
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()
        self.request_timestamps: deque = deque()
        self.total_requests = 0
        self.throttled_count = 0

    def update_rpm(self, rpm: int):
        self.rpm = rpm
        # refill tokens lazily on next acquire

    async def acquire(self, tokens: int = 1) -> float:
        """
        Acquires tokens, returns wait_time if throttled else 0.
        Sleeps if necessary to respect rate limit.
        """
        async with self.lock:
            now = time.monotonic()
            # Clean timestamps older than 60s
            while self.request_timestamps and now - self.request_timestamps[0] > 60:
                self.request_timestamps.popleft()

            if len(self.request_timestamps) + tokens <= self.rpm:
                for _ in range(tokens):
                    self.request_timestamps.append(now)
                self.total_requests += 1
                return 0.0

            # Need to wait until oldest timestamp expires
            oldest = self.request_timestamps[0]
            wait = 60 - (now - oldest) + 0.05
            self.throttled_count += 1
            return wait

    async def wait_and_acquire(self, tokens: int = 1):
        while True:
            wait = await self.acquire(tokens)
            if wait <= 0:
                return
            await asyncio.sleep(wait)

    def stats(self):
        now = time.monotonic()
        while self.request_timestamps and now - self.request_timestamps[0] > 60:
            self.request_timestamps.popleft()
        return {
            "rpm": self.rpm,
            "used_in_window": len(self.request_timestamps),
            "remaining": max(0, self.rpm - len(self.request_timestamps)),
            "total_requests": self.total_requests,
            "throttled": self.throttled_count,
        }

# Global instance
rate_limiter = TokenBucketRateLimiter()
