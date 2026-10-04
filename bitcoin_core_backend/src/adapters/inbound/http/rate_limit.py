"""Rate-limiting policies used by the HTTP adapter.

Keeping these implementations outside the Flask composition root makes the
policy independently testable and keeps infrastructure choices injectable.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any


class FixedWindowLimiter:
    """Thread-safe in-process rate limiter using a rolling event window."""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        """Create a limiter with an allowance and window duration in seconds."""
        self._limit = limit
        self._window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record a request for ``key`` and report whether it is within the limit."""
        with self._lock:
            now = time.monotonic()
            events = self._events[key]
            while events and now - events[0] > self._window_seconds:
                events.popleft()
            if len(events) >= self._limit:
                return False
            events.append(now)
            return True


class RedisRateLimiter:
    """Redis-backed distributed limiter for multi-worker deployments.

    Redis connectivity or command failures follow the configured ``fail_open``
    policy, making the availability/security tradeoff explicit at construction.
    """

    def __init__(self, redis_url: str, limit_per_minute: int, *, fail_open: bool):
        """Configure Redis URL, per-minute request ceiling, and failure policy."""
        self._redis_url = redis_url
        self._limit = max(1, int(limit_per_minute))
        self._fail_open = fail_open
        self._redis: Any = None

    def _ensure_redis(self) -> Any:
        """Lazily connect and ping Redis; cache ``False`` after initialization failure."""
        if self._redis is not None:
            return self._redis
        try:
            import redis as redis_lib
            self._redis = redis_lib.Redis.from_url(self._redis_url, socket_timeout=5)
            self._redis.ping()
        except Exception:
            self._redis = False
        return self._redis

    def allow(self, key: str) -> bool:
        """Increment a key-specific minute counter and apply the failure policy."""
        redis_client = self._ensure_redis()
        if redis_client is False:
            return self._fail_open
        try:
            redis_key = f"ratelimit:bitcoin:{key}:60"
            count = redis_client.incr(redis_key)
            if count == 1:
                redis_client.expire(redis_key, 60)
            return count <= self._limit
        except Exception:
            return self._fail_open
