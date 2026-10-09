"""Кэширование результатов инструментов в Redis.

Агент задаёт внешним системам одни и те же вопросы повторно: два расследования
одного сервиса с разницей в минуты получают то же окно метрик, тот же список
деплоев, те же агрегированные ошибки. Каждое из них — это round trip к системе,
которая принадлежит кому-то другому и имеет свои лимиты.

Три правила не дают этому стать опасным.

Кэшируется только чтение. Запись имеет эффект, а эффект нельзя отдать из кэша.
Исполнитель решает по классу доступа, а не по имени.

Записи быстро истекают. Данные мониторинга за окно, включающее «сейчас», ещё
меняются; минута устаревания — разумная плата за round trip, час — нет.

Отсутствующий Redis — не ошибка. Каждая операция деградирует до промаха.
Кэширование — это оптимизация, а оптимизация, способная уронить систему, — это
обуза.
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
    """Стабильный ключ для одного вопроса, заданного одному инструменту.

    Хэшируется, а не выписывается целиком: аргументы содержат ISO-отметки времени
    и имена сервисов, а ключ, собранный конкатенацией, был бы длинным, неудобным
    для чтения в redis-cli и иногда недопустимым.
    """
    payload = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"{KEY_PREFIX}:{tool}:{digest}"


class NullCache:
    """То, что использует агент при выключенном кэшировании. Любой поиск — промах."""

    async def get(self, key: str) -> dict[str, Any] | None:
        return None

    async def set(self, key: str, value: dict[str, Any], *, ttl: int) -> None:
        return None


class MemoryToolCache:
    """Локальный для процесса TTL-кэш, используемый локальным профилем без зависимостей."""

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
    """Кэш на базе Redis, который никогда не выбрасывает исключение у вызывающего.

    Сбои логируются один раз на операцию и трактуются как промах. Агент не должен
    заботиться о том, поднят ли Redis; расследование с холодным кэшем медленнее, и
    это всё последствие.
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
            data = json.loads(raw)
        except (TypeError, ValueError):
            # Повреждённая запись считается промахом, а не сбоем; хранить её незачем.
            log.warning("cache.corrupt_entry", key=key)
            return None
        return data if isinstance(data, dict) else None

    async def set(self, key: str, value: dict[str, Any], *, ttl: int) -> None:
        try:
            await self._client.set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:
            log.warning("cache.unavailable", operation="set", error=f"{type(exc).__name__}: {exc}")


def build_cache(settings: Any) -> ToolCache:
    """Настроенный кэш или пустышка.

    Создание клиента не устанавливает соединение, поэтому недоступный Redis
    проявляется как предупреждения и промахи во время вызова, а не как сбой
    запуска.
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
