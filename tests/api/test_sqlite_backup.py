"""Backup and restore keep local product data recoverable."""

import sqlite3

import pytest

from app.db.backup import create_backup, restore_backup
from app.db.sqlite import DatabaseIntegrityError


def _database(path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE data (value TEXT)")
        connection.execute("INSERT INTO data VALUES (?)", (value,))


def _value(path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM data").fetchone()[0]


def test_backup_and_restore_preserve_the_displaced_database(tmp_path):
    database = tmp_path / "agent.db"
    backup = tmp_path / "backups" / "agent.db"
    _database(database, "before")
    create_backup(database, backup)

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE data SET value = 'after'")

    displaced = restore_backup(backup, database)

    assert _value(database) == "before"
    assert displaced is not None
    assert _value(displaced) == "after"


def test_invalid_backup_never_replaces_the_current_database(tmp_path):
    database = tmp_path / "agent.db"
    backup = tmp_path / "broken.db"
    _database(database, "current")
    backup.write_bytes(b"broken")

    with pytest.raises(DatabaseIntegrityError):
        restore_backup(backup, database)

    assert _value(database) == "current"
