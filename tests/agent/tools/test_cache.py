"""Кэширование результатов инструментов: что можно отдавать, а что никогда."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from app.agent.guardrails import Guardrails
from app.agent.tools.base import AgentTool, ToolAccess, ToolRegistry
from app.agent.tools.catalog import build_registry
from app.agent.tools.executor import ToolExecutor, ToolRequest
from app.services.cache import MemoryToolCache, NullCache, RedisToolCache, cache_key

WINDOW = {
    "start": datetime(2026, 3, 17, 14, 0, tzinfo=UTC),
    "end": datetime(2026, 3, 17, 15, 0, tzinfo=UTC),
}


class _Args(BaseModel):
    n: int = 0


class _Result(BaseModel):
    calls: int


class FakeRedis:
    """Достаточная часть клиентского API, чтобы прогнать кэш, плюс переключатель сбоя."""

    def __init__(self, *, broken: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.broken = broken
        self.sets: list[tuple[str, int]] = []

    async def get(self, key: str):
        if self.broken:
            raise ConnectionError("redis is down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        if self.broken:
            raise ConnectionError("redis is down")
        self.store[key] = value
        self.sets.append((key, ex or 0))


def _counting_tool(counter: dict[str, int], access: ToolAccess) -> AgentTool:
    async def handler(args: _Args) -> _Result:
        counter["calls"] += 1
        return _Result(calls=counter["calls"])

    return AgentTool(
        name="counted",
        description="counts how many times it actually ran",
        args_schema=_Args,
        result_schema=_Result,
        access=access,
        handler=handler,
        render=lambda r: f"ran {r.calls} time(s)",
    )


def _executor(tool: AgentTool, cache: Any, **policy: Any) -> ToolExecutor:
    return ToolExecutor(
        ToolRegistry([tool]),
        Guardrails(max_identical_calls=99, **policy),
        cache=cache,
        cache_ttl=30,
    )


# ── Ключи ────────────────────────────────────────────────────────────────────


def test_the_key_depends_on_the_question_not_its_spelling():
    assert cache_key("t", {"a": 1, "b": 2}) == cache_key("t", {"b": 2, "a": 1})
    assert cache_key("t", {"a": 1}) != cache_key("t", {"a": 2})
    assert cache_key("t", {"a": 1}) != cache_key("other", {"a": 1})


def test_keys_are_namespaced_so_a_shared_redis_cannot_collide():
    assert cache_key("get_commits", {}).startswith("aoa:tool:get_commits:")


# ── Поведение ────────────────────────────────────────────────────────────────


async def test_a_repeated_read_is_served_without_touching_the_provider():
    counter = {"calls": 0}
    executor = _executor(_counting_tool(counter, ToolAccess.READ), RedisToolCache(FakeRedis()))
    request = ToolRequest(tool="counted", arguments={"n": 1})

    first = await executor.execute(request)
    second = await executor.execute(request)

    assert counter["calls"] == 1, "the provider ran once"
    assert first.ok and second.ok
    assert first.result.calls == second.result.calls == 1
    assert first.record.cached is False
    assert second.record.cached is True


async def test_a_different_question_is_not_a_cache_hit():
    counter = {"calls": 0}
    executor = _executor(_counting_tool(counter, ToolAccess.READ), RedisToolCache(FakeRedis()))

    await executor.execute(ToolRequest(tool="counted", arguments={"n": 1}))
    await executor.execute(ToolRequest(tool="counted", arguments={"n": 2}))
    assert counter["calls"] == 2


async def test_writes_are_never_cached():
    """У записи есть эффект, а эффект нельзя отдать из кэша."""
    counter = {"calls": 0}
    redis = FakeRedis()
    executor = _executor(
        _counting_tool(counter, ToolAccess.WRITE), RedisToolCache(redis), allow_write=True
    )
    request = ToolRequest(tool="counted", arguments={"n": 1})

    await executor.execute(request)
    await executor.execute(request)

    assert counter["calls"] == 2, "both writes reached the provider"
    assert redis.store == {}, "and nothing was stored"


async def test_entries_expire_quickly_enough_to_stay_honest():
    """Данные мониторинга для окна, включающего 'сейчас', ещё меняются."""
    redis = FakeRedis()
    executor = _executor(_counting_tool({"calls": 0}, ToolAccess.READ), RedisToolCache(redis))
    await executor.execute(ToolRequest(tool="counted", arguments={"n": 1}))
    assert redis.sets[0][1] == 30


async def test_the_local_cache_expires_entries_without_redis(monkeypatch):
    now = 100.0
    monkeypatch.setattr("app.services.cache.time.monotonic", lambda: now)
    cache = MemoryToolCache()

    await cache.set("key", {"answer": 42}, ttl=30)
    assert await cache.get("key") == {"answer": 42}

    now = 131.0
    assert await cache.get("key") is None


async def test_an_unreachable_cache_degrades_to_a_miss():
    """Кэширование — оптимизация; оно не должно уметь ломать запуск."""
    counter = {"calls": 0}
    executor = _executor(
        _counting_tool(counter, ToolAccess.READ), RedisToolCache(FakeRedis(broken=True))
    )
    request = ToolRequest(tool="counted", arguments={"n": 1})

    first = await executor.execute(request)
    second = await executor.execute(request)

    assert first.ok and second.ok
    assert counter["calls"] == 2, "every call went to the provider, and none failed"


async def test_a_corrupt_entry_is_a_miss_not_a_crash():
    counter = {"calls": 0}
    redis = FakeRedis()
    tool = _counting_tool(counter, ToolAccess.READ)
    executor = _executor(tool, RedisToolCache(redis))
    request = ToolRequest(tool="counted", arguments={"n": 1})
    redis.store[cache_key("counted", {"n": 1})] = "}{ not json"

    invocation = await executor.execute(request)
    assert invocation.ok
    assert counter["calls"] == 1


async def test_a_cached_value_that_no_longer_fits_the_schema_is_refetched():
    """Инструмент сменил форму с момента записи элемента."""
    counter = {"calls": 0}
    redis = FakeRedis()
    redis.store[cache_key("counted", {"n": 1})] = '{"unexpected": "shape"}'
    executor = _executor(_counting_tool(counter, ToolAccess.READ), RedisToolCache(redis))

    invocation = await executor.execute(ToolRequest(tool="counted", arguments={"n": 1}))
    assert invocation.ok
    assert counter["calls"] == 1


async def test_caching_is_off_by_default_for_a_bare_executor():
    counter = {"calls": 0}
    executor = _executor(_counting_tool(counter, ToolAccess.READ), NullCache())
    request = ToolRequest(tool="counted", arguments={"n": 1})

    await executor.execute(request)
    await executor.execute(request)
    assert counter["calls"] == 2


# ── С настоящим каталогом ───────────────────────────────────────────────────


async def test_a_cached_metric_query_returns_the_same_typed_series(monitoring, code, logs):
    registry = build_registry(monitoring, code, logs)
    executor = ToolExecutor(
        registry, Guardrails(max_identical_calls=99), cache=RedisToolCache(FakeRedis())
    )
    request = ToolRequest(
        tool="get_service_metrics",
        arguments={"service": "billing-service", "metric": "error_rate"},
    )

    fresh = await executor.execute(request, defaults=WINDOW)
    cached = await executor.execute(request, defaults=WINDOW)

    assert cached.record.cached is True
    assert cached.result.series.points == fresh.result.series.points
    assert cached.digest == fresh.digest


@pytest.mark.parametrize("enabled", [True, False])
def test_the_cache_can_be_switched_off_by_configuration(enabled):
    from app.core.config import Settings
    from app.services.cache import build_cache

    cache = build_cache(Settings(cache_enabled=enabled))
    assert isinstance(cache, NullCache) is (not enabled)


def test_the_local_profile_uses_memory_cache_by_default():
    from app.core.config import Settings
    from app.services.cache import build_cache

    settings = Settings(cache_enabled=True, cache_backend="memory")
    assert isinstance(build_cache(settings), MemoryToolCache)
