"""Where a paused run is kept while it waits for a person.

The approval gate is only as durable as the checkpointer behind it. With an
in-memory saver, a restart between the proposal and the decision loses the
investigation: the run row survives, the workflow does not, and the approval
endpoint has nothing to resume. Local installations use SQLite; server
deployments can select PostgreSQL.

Falling back to memory is deliberate and noisy. A missing database should not
stop the service from starting — a read-only investigation still works — but
it changes a documented guarantee, so it is logged at warning level and
reported by the health endpoint rather than quietly tolerated.
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
    """The checkpointer speaks psycopg, the application speaks asyncpg."""
    return str(settings.postgres_dsn).replace("postgresql+asyncpg://", "postgresql://")


async def startup(settings: Settings | None = None) -> BaseCheckpointSaver:
    """Open the checkpointer, preferring the durable one."""
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
    """The saver the graph was compiled with, creating a fallback if needed."""
    global _saver
    if _saver is None:
        _saver = InMemorySaver(serde=agent_serializer())
    return _saver


def is_durable() -> bool:
    """Whether a paused approval would survive a restart, right now."""
    return _durable
