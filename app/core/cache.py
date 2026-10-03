"""Small cache facade: Redis when REDIS_URL is set, otherwise in-process memory.

Used for (1) read-through caching of catalogue endpoints and (2) rate-limit counters.
Every Redis failure degrades gracefully (cache miss / fail-open) so Redis being down
never takes the API down.
"""
import hashlib
import json
import logging
import threading
import time
from typing import Any, Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class Backend(Protocol):
    name: str

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl: int) -> None: ...
    def incr(self, key: str, ttl: int) -> int: ...
    def ttl(self, key: str) -> int: ...
    def clear(self) -> None: ...


class MemoryBackend:
    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str) -> tuple[str, float | None] | None:
        item = self._data.get(key)
        if item and item[1] is not None and item[1] <= time.monotonic():
            del self._data[key]
            return None
        return item

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._live(key)
            return item[0] if item else None

    def set(self, key: str, value: str, ttl: int) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic() + ttl if ttl else None)

    def incr(self, key: str, ttl: int) -> int:
        with self._lock:
            item = self._live(key)
            if item is None:
                self._data[key] = ("1", time.monotonic() + ttl)
                return 1
            value = int(item[0]) + 1
            self._data[key] = (str(value), item[1])
            return value

    def ttl(self, key: str) -> int:
        with self._lock:
            item = self._live(key)
            if not item or item[1] is None:
                return 0
            return max(int(item[1] - time.monotonic()), 0)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class RedisBackend:
    name = "redis"

    def __init__(self, url: str) -> None:
        import redis

        self._r = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1
        )

    def get(self, key: str) -> str | None:
        return self._r.get(key)

    def set(self, key: str, value: str, ttl: int) -> None:
        self._r.set(key, value, ex=ttl or None)

    def incr(self, key: str, ttl: int) -> int:
        pipe = self._r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        return int(pipe.execute()[0])

    def ttl(self, key: str) -> int:
        return max(int(self._r.ttl(key)), 0)

    def clear(self) -> None:
        self._r.flushdb()


class Cache:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    # -- JSON helpers -------------------------------------------------------
    def get_json(self, key: str) -> Any | None:
        try:
            raw = self.backend.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            logger.warning("cache_get_failed", extra={"key": key})
            return None

    def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            self.backend.set(key, json.dumps(value), ttl or get_settings().cache_ttl_seconds)
        except Exception:
            logger.warning("cache_set_failed", extra={"key": key})

    # -- namespace versioning: bump() invalidates every key of a namespace ---
    def _version(self, namespace: str) -> str:
        try:
            return self.backend.get(f"ver:{namespace}") or "0"
        except Exception:
            return "0"

    def bump(self, namespace: str) -> None:
        try:
            self.backend.incr(f"ver:{namespace}", 10 * 365 * 24 * 3600)
        except Exception:
            logger.warning("cache_bump_failed", extra={"namespace": namespace})

    def key(self, namespace: str, *parts: Any) -> str:
        digest = hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]
        return f"{namespace}:v{self._version(namespace)}:{digest}"

    # -- counters (rate limiting) ------------------------------------------
    def hit(self, key: str, window: int) -> tuple[int, int]:
        """Increment a fixed-window counter. Returns (count, seconds_until_reset).
        Fails open (count=0) if the backend is unavailable."""
        try:
            count = self.backend.incr(key, window)
            return count, self.backend.ttl(key) or window
        except Exception:
            logger.warning("rate_limit_backend_failed", extra={"key": key})
            return 0, window


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        url = get_settings().redis_url
        backend: Backend = RedisBackend(url) if url else MemoryBackend()
        _cache = Cache(backend)
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None
