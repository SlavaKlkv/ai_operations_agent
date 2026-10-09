"""Согласованное резервное копирование и восстанавливаемое восстановление
для локальной базы SQLite."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from pathlib import Path

from app.db.sqlite import verify_integrity


def create_backup(source: Path, destination: Path) -> Path:
    """Create a consistent snapshot even while the source uses WAL."""
    verify_integrity(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination)
    try:
        # `with sqlite3.connect(...)` только фиксирует транзакцию и НЕ закрывает
        # соединение. На Windows открытый файл нельзя заменить, поэтому закрываем
        # соединения явно, иначе перенос копии падает с PermissionError.
        with (
            contextlib.closing(sqlite3.connect(source)) as source_connection,
            contextlib.closing(sqlite3.connect(temporary)) as destination_connection,
        ):
            source_connection.backup(destination_connection)
        verify_integrity(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def restore_backup(backup: Path, destination: Path) -> Path | None:
    """Атомарно восстановить проверенный снимок и сохранить вытесненный файл."""
    verify_integrity(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    restored = _temporary_path(destination)
    displaced = destination.with_suffix(f"{destination.suffix}.before-restore")
    try:
        with (
            contextlib.closing(sqlite3.connect(backup)) as source_connection,
            contextlib.closing(sqlite3.connect(restored)) as destination_connection,
        ):
            source_connection.backup(destination_connection)
        verify_integrity(restored)
        if destination.exists():
            os.replace(destination, displaced)
        os.replace(restored, destination)
    except Exception:
        if displaced.exists() and not destination.exists():
            os.replace(displaced, destination)
        raise
    finally:
        restored.unlink(missing_ok=True)
    return displaced if displaced.exists() else None


def _temporary_path(destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path
