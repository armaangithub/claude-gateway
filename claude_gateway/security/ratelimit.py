"""Token-bucket rate limiting (per identity).

A classic token bucket: ``capacity`` = burst, refilled at ``per_min/60`` tokens
per second. Each request costs one token. Cheap, smooth, and bursty-tolerant.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__("rate limit exceeded")
        self.retry_after = retry_after


@dataclass
class _Bucket:
    tokens: float
    last: float


class RateLimiter:
    def __init__(self, per_min: int, burst: int) -> None:
        self.rate = max(0.0, per_min / 60.0)  # tokens per second
        self.capacity = float(max(1, burst))
        self.enabled = per_min > 0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, identity: str, cost: float = 1.0) -> None:
        if not self.enabled:
            return
        now = time.time()
        with self._lock:
            b = self._buckets.get(identity)
            if b is None:
                b = _Bucket(tokens=self.capacity, last=now)
                self._buckets[identity] = b
            # Refill.
            elapsed = now - b.last
            b.tokens = min(self.capacity, b.tokens + elapsed * self.rate)
            b.last = now
            if b.tokens < cost:
                needed = cost - b.tokens
                retry_after = needed / self.rate if self.rate > 0 else 60.0
                raise RateLimitExceeded(retry_after)
            b.tokens -= cost

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {k: round(v.tokens, 2) for k, v in self._buckets.items()}
