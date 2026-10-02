"""In-process sliding-window rate limiter (stdlib only).

Good for the single-process deployment this project ships with. If you ever run
more than one replica, move the counters to Redis.
"""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 50_000) -> None:
        self.limit = int(limit)
        self.window = float(window_seconds)
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._last_gc = time.monotonic()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic() if now is None else now
        self._maybe_gc(now)
        q = self._hits.get(key)
        if q is None:
            q = self._hits[key] = deque()
        cutoff = now - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        q = self._hits.get(key)
        if not q:
            return 0
        return max(1, int(q[0] + self.window - now) + 1)

    def _maybe_gc(self, now: float) -> None:
        if now - self._last_gc < 60.0 and len(self._hits) < self.max_keys:
            return
        self._last_gc = now
        cutoff = now - self.window
        stale = [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]
        for k in stale:
            self._hits.pop(k, None)
        if len(self._hits) >= self.max_keys:  # hostile key flood: drop everything, stay alive
            self._hits.clear()
