"""In-memory token buckets with a bounded number of tracked keys."""

from __future__ import annotations

import math
import time
from collections import OrderedDict


class RateLimiter:
    """Allow `burst` requests at once, refilled at `rate` per second and key.

    Least recently used keys are forgotten beyond `max_keys`, which can only
    make the limiter more lenient, never use unbounded memory. Limits are per
    process; run one proxy process or accept proportionally higher limits.
    """

    def __init__(self, rate: float, burst: int, max_keys: int = 100_000) -> None:
        self.rate = rate
        self.burst = burst
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def acquire(self, key: str, now: float | None = None) -> int:
        """Take a token; return 0, or the seconds until the next one is available."""
        current = time.monotonic() if now is None else now
        tokens, updated = self._buckets.pop(key, (float(self.burst), current))
        tokens = min(float(self.burst), tokens + (current - updated) * self.rate)
        retry_after = 0
        if tokens >= 1:
            tokens -= 1
        else:
            retry_after = max(1, math.ceil((1 - tokens) / self.rate))
        self._buckets[key] = (tokens, current)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        return retry_after
