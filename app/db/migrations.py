"""Применять миграции базы до того, как API начнёт принимать запросы."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import Settings
from app.db.backup import create_backup
from app.db.sqlite import verify_integrity

log = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Копия, снятая непосредственно перед применением отставших миграций.
#: Имя предсказуемо и перезаписывается на каждом обновлении, поэтому каталог
#: данных не накапливает десятки снимков.
PRE_UPGRADE_SUFFIX = ".pre-upgrade.bak"


def _build_config(settings: Settings) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["sqlalchemy_url"] = settings.database_dsn
    return config


def _current_revision(path: Path) -> str | None:
    """Прочитать применённую ревизию, не изменяя базу."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError:
        # Таблица версии ещё не создана; пустую схему обрабатывает `verify_integrity`.
        return None
    return row[0] if row else None


def _backup_before_upgrade(path: Path, config: Config) -> None:
    """Снять снимок локальной базы непосредственно перед отставшим изменением схемы.

    Первая установка ничего не теряет, поэтому копия создаётся только когда в
    существующей базе осталось неприменённое обновление схемы. Так у пользователя
    всегда есть согласованный откат к состоянию непосредственно до обновления.
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    head = ScriptDirectory.from_config(config).get_current_head()
    current = _current_revision(path)
    if current == head:
        return
    destination = path.with_name(path.name + PRE_UPGRADE_SUFFIX)
    create_backup(path, destination)
    log.info(
        "database.pre_upgrade_backup",
        from_revision=current,
        to_revision=head,
        destination=str(destination),
    )


def _upgrade(settings: Settings) -> None:
    config = _build_config(settings)
    if settings.storage_backend == "sqlite":
        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        verify_integrity(settings.sqlite_path)
        _backup_before_upgrade(settings.sqlite_path, config)
    command.upgrade(config, "head")


async def migrate(settings: Settings) -> None:
    """Выполнить блокирующую работу Alembic вне цикла событий."""
    await asyncio.to_thread(_upgrade, settings)
