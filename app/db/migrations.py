"""Apply database migrations before the API starts accepting requests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import Settings
from app.db.sqlite import verify_integrity

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _upgrade(settings: Settings) -> None:
    if settings.storage_backend == "sqlite":
        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        verify_integrity(settings.sqlite_path)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["sqlalchemy_url"] = settings.database_dsn
    command.upgrade(config, "head")


async def migrate(settings: Settings) -> None:
    """Run blocking Alembic work outside the event loop."""
    await asyncio.to_thread(_upgrade, settings)
