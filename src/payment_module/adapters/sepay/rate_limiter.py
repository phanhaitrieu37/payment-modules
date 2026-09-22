"""One token bucket per process for the SePay API.

SePay limits API v2 to 3 requests per second **per IP**, so every merchant of an
installation shares the quota: all readers of a process share one limiter (default 2 req/s,
below the provider limit). Fairness between merchants comes from the reconcile scheduler's
round robin, not from this limiter.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

type Monotonic = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]


class ProcessRateLimiter:
    """``acquire()`` waits until a request may be sent; burst is ``capacity`` requests."""

    def __init__(
        self,
        rate_per_second: float,
        *,
        capacity: float = 1.0,
        monotonic: Monotonic = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate must be positive")
        self.rate_per_second = rate_per_second
        self._capacity = capacity
        self._tokens = capacity
        self._monotonic = monotonic
        self._sleep = sleep
        self._updated = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._monotonic()
                elapsed = max(now - self._updated, 0.0)
                self._tokens = min(self._capacity, self._tokens + elapsed * self.rate_per_second)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self.rate_per_second)


_process_limiter: ProcessRateLimiter | None = None


def process_rate_limiter(rate_per_second: float) -> ProcessRateLimiter:
    """The process-wide limiter, created on first use with ``rate_per_second``.

    Later callers get the same instance whatever rate they pass, so two readers can never
    double the quota of one IP.
    """
    global _process_limiter
    if _process_limiter is None:
        _process_limiter = ProcessRateLimiter(rate_per_second)
    return _process_limiter
