"""Перед применением отложенного обновления схемы снимается снимок.

Веха 7: обновление не должно уничтожать данные. Если локальная база отстала от
head, перед применением миграций снимается согласованная копия, из которой
пользователь может откатиться на состояние непосредственно до обновления.
"""

from __future__ import annotations

import asyncio
import sqlite3

from alembic import command
from alembic.config import Config

from app.core.config import Settings
from app.db.migrations import PRE_UPGRADE_SUFFIX, migrate

#: Одна ревизия назад от head — так тест не привязан к конкретным идентификаторам.
ONE_REVISION_BACK = "-1"


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        storage_backend="sqlite",
        sqlite_path=tmp_path / "agent.db",
    )  # type: ignore[call-arg]


def _alembic_config(database) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.attributes["sqlalchemy_url"] = f"sqlite+aiosqlite:///{database}"
    return config


def _backup_path(settings: Settings):
    return settings.sqlite_path.with_name(settings.sqlite_path.name + PRE_UPGRADE_SUFFIX)


def _revision(path) -> str | None:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


async def test_pending_upgrade_is_snapshotted_before_migrating(tmp_path):
    settings = _settings(tmp_path)
    await migrate(settings)

    # Эмулируем старую установку: одна миграция ещё не применена.
    # `env.py` вызывает `asyncio.run`, поэтому блокирующий откат уводим в поток.
    await asyncio.to_thread(
        command.downgrade, _alembic_config(settings.sqlite_path), ONE_REVISION_BACK
    )
    outdated = _revision(settings.sqlite_path)

    await migrate(settings)

    backup = _backup_path(settings)
    assert backup.exists()
    # Копия фиксирует состояние до обновления, а рабочая база — после.
    assert _revision(backup) == outdated
    assert _revision(settings.sqlite_path) != outdated


async def test_first_install_creates_no_snapshot(tmp_path):
    settings = _settings(tmp_path)

    await migrate(settings)

    assert not _backup_path(settings).exists()


async def test_startup_already_at_head_creates_no_snapshot(tmp_path):
    settings = _settings(tmp_path)
    await migrate(settings)

    # Повторный запуск на актуальной схеме не должен создавать копию каждый раз.
    await migrate(settings)

    assert not _backup_path(settings).exists()
