"""Настройки безопасности SQLite и проверки целостности при запуске."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

BUSY_TIMEOUT_MS = 5_000


class DatabaseIntegrityError(RuntimeError):
    """Существующая локальная база нечитаема или не прошла проверки SQLite."""


def configure_engine(engine: AsyncEngine) -> None:
    """Позволить соединениям API и чекпоинтера безопасно делить локальную базу."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def configure_checkpointer(connection: Any) -> None:
    await connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await connection.execute("PRAGMA journal_mode=WAL")
    await connection.execute("PRAGMA foreign_keys=ON")
    await connection.commit()


def verify_integrity(path: Path) -> None:
    """Отклонить повреждённую существующую базу, не изменяя и не заменяя её."""
    if not path.exists() or path.stat().st_size == 0:
        return
    try:
        # Закрываем соединение явно: контекстный менеджер sqlite3 не закрывает его,
        # а на Windows открытый файл базы не удаётся заменить или удалить.
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise DatabaseIntegrityError(f"SQLite database is unreadable: {path}") from exc
    if result != ("ok",):
        detail = result[0] if result else "no result"
        raise DatabaseIntegrityError(f"SQLite integrity check failed for {path}: {detail}")
