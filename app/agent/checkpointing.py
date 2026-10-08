"""Где хранится приостановленный запуск, пока он ждёт человека.

Шлюз подтверждения настолько же надёжен, насколько надёжен чекпоинтер за ним.
При хранении в памяти перезапуск между предложением и решением теряет
расследование: строка запуска выживает, рабочий процесс — нет, и у эндпоинта
подтверждения нечего возобновлять. Локальные установки используют SQLite;
серверные деплои могут выбрать PostgreSQL.

Возврат к памяти намеренный и шумный. Отсутствующая база данных не должна
мешать сервису запуститься — исследование только для чтения всё равно
работает — но это меняет документированную гарантию, поэтому фиксируется на
уровне warning и сообщается эндпоинтом здоровья, а не тихо игнорируется.
"""

from __future__ import annotations

from contextlib import AsyncExitStack

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.serde import agent_serializer
from app.core.config import Settings, get_settings
from app.db.sqlite import configure_checkpointer

log = structlog.get_logger(__name__)

_stack = AsyncExitStack()
_saver: BaseCheckpointSaver | None = None
_durable = False


def _postgres_dsn(settings: Settings) -> str:
    """Чекпоинтер говорит на psycopg, приложение — на asyncpg."""
    return str(settings.postgres_dsn).replace("postgresql+asyncpg://", "postgresql://")


async def startup(settings: Settings | None = None) -> BaseCheckpointSaver:
    """Открыть чекпоинтер, предпочитая надёжный."""
    global _saver, _durable
    if _saver is not None:
        return _saver

    settings = settings or get_settings()
    if settings.checkpointer == "memory":
        _saver, _durable = InMemorySaver(serde=agent_serializer()), False
        log.info("checkpointer.ready", kind="memory", durable=False)
        return _saver

    if settings.checkpointer == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite_saver = await _stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(settings.sqlite_path))
        )
        await configure_checkpointer(sqlite_saver.conn)
        sqlite_saver.serde = agent_serializer()
        await sqlite_saver.setup()
        _saver, _durable = sqlite_saver, True
        log.info("checkpointer.ready", kind="sqlite", durable=True)
        return sqlite_saver

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        postgres_saver = await _stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(_postgres_dsn(settings))
        )
        postgres_saver.serde = agent_serializer()
        await postgres_saver.setup()
    except Exception as exc:
        log.warning(
            "checkpointer.degraded",
            error=f"{type(exc).__name__}: {exc}",
            consequence="a run awaiting approval will not survive a restart",
        )
        _saver, _durable = InMemorySaver(serde=agent_serializer()), False
        return _saver

    _saver, _durable = postgres_saver, True
    log.info("checkpointer.ready", kind="postgres", durable=True)
    return postgres_saver


async def shutdown() -> None:
    global _saver, _durable
    await _stack.aclose()
    _saver, _durable = None, False


def get_saver() -> BaseCheckpointSaver:
    """Хранитель, с которым был скомпилирован граф; при необходимости создаёт
    запасной."""
    global _saver
    if _saver is None:
        _saver = InMemorySaver(serde=agent_serializer())
    return _saver


def is_durable() -> bool:
    """Переживёт ли приостановленное подтверждение перезапуск прямо сейчас."""
    return _durable
