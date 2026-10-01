"""SQLite safety settings and startup integrity checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

BUSY_TIMEOUT_MS = 5_000


class DatabaseIntegrityError(RuntimeError):
    """The existing local database is unreadable or failed SQLite checks."""


def configure_engine(engine: AsyncEngine) -> None:
    """Allow API and checkpointer connections to share the local database safely."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def configure_checkpointer(connection) -> None:
    await connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await connection.execute("PRAGMA journal_mode=WAL")
    await connection.execute("PRAGMA foreign_keys=ON")
    await connection.commit()


def verify_integrity(path: Path) -> None:
    """Reject a damaged existing database without modifying or replacing it."""
    if not path.exists() or path.stat().st_size == 0:
        return
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise DatabaseIntegrityError(f"SQLite database is unreadable: {path}") from exc
    if result != ("ok",):
        detail = result[0] if result else "no result"
        raise DatabaseIntegrityError(f"SQLite integrity check failed for {path}: {detail}")
