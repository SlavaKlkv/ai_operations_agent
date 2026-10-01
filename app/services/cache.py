"""Caching tool results in Redis.

The agent asks external systems the same questions repeatedly: two
investigations into the same service minutes apart fetch the same metric
window, the same deployment list, the same aggregated errors. Each of those is
a round trip to a system that belongs to someone else and has its own rate
limits.

Three rules keep this from being dangerous.

*Only reads are cached.* A write has an effect, and an effect cannot be served
from a cache. The executor decides by access class, not by name.

*Entries expire quickly.* Monitoring data for a window that includes "now" is
still moving; a minute of staleness is a reasonable trade for the round trip,
an hour is not.

*A missing Redis is not an error.* Every operation degrades to a miss. Caching
is an optimisation, and an optimisation that can take the system down is a
liability.
"""

from __future__ import annotations

import hashlib
import json
import time
from asyncio import Lock
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

#: Пространство имён защищает общий Redis от конфликтов с другими приложениями
#: и позволяет удалить весь кэш одним шаблоном.
KEY_PREFIX = "aoa:tool"


class ToolCache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], *, ttl: int) -> None: ...


def cache_key(tool: str, arguments: dict[str, Any]) -> str:
    """A stable key for one question asked of one tool.

    Hashed rather than spelled out: arguments contain ISO timestamps and
    service names, and a key built by concatenation would be long, awkward to
    read in ``redis-cli``, and occasionally illegal.
    """
    payload = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"{KEY_PREFIX}:{tool}:{digest}"


class NullCache:
    """What the agent uses when caching is off. Every lookup is a miss."""

    async def get(self, key: str) -> dict[str, Any] | None:
        return None

    async def set(self, key: str, value: dict[str, Any], *, ttl: int) -> None:
        return None


class MemoryToolCache:
    """Process-local TTL cache used by the zero-dependency local profile."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = Lock()

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= time.monotonic():
                self._entries.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: dict[str, Any], *, ttl: int) -> None:
        async with self._lock:
            self._entries[key] = (time.monotonic() + ttl, value)


class RedisToolCache:
    """Redis-backed cache that never raises at the caller.

    Failures are logged once per operation and treated as a miss. The agent
    must not care whether Redis is up; an investigation with a cold cache is
    slower, and that is the entire consequence.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(key)
        except Exception as exc:
            log.warning("cache.unavailable", operation="get", error=f"{type(exc).__name__}: {exc}")
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # Повреждённая запись считается промахом, а не сбоем; хранить её незачем.
            log.warning("cache.corrupt_entry", key=key)
            return None

    async def set(self, key: str, value: dict[str, Any], *, ttl: int) -> None:
        try:
            await self._client.set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:
            log.warning("cache.unavailable", operation="set", error=f"{type(exc).__name__}: {exc}")


def build_cache(settings: Any) -> ToolCache:
    """The configured cache, or a no-op one.

    Constructing the client does not connect, so an unreachable Redis surfaces
    as warnings and misses at call time rather than as a failure to start.
    """
    if not settings.cache_enabled:
        return NullCache()
    if settings.cache_backend == "memory":
        return MemoryToolCache()
    try:
        from redis.asyncio import Redis

        return RedisToolCache(Redis.from_url(str(settings.redis_url), decode_responses=True))
    except Exception as exc:
        log.warning("cache.disabled", error=f"{type(exc).__name__}: {exc}")
        return NullCache()
