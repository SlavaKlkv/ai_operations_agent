"""Защита от классического расхождения: модели изменились, миграцию забыли."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.base import Base, build_engine
from app.db.migrations import migrate
from app.db.sqlite import DatabaseIntegrityError
from app.services.run_store import create_run

#: SQLite и PostgreSQL могут обоснованно различаться деталями индексов и типов,
#: но отсутствие или наличие лишней таблицы либо столбца допустимым не бывает.
STRUCTURAL = {"add_table", "remove_table", "add_column", "remove_column"}


@pytest.fixture
def migrated_sqlite(tmp_path):
    db = tmp_path / "migrations.db"
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.attributes["sqlalchemy_url"] = f"sqlite+aiosqlite:///{db}"
    command.upgrade(config, "head")
    return f"sqlite:///{db}"


def test_migrations_reproduce_the_model_metadata(migrated_sqlite):
    engine = create_engine(migrated_sqlite)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    structural = [d for d in diff if isinstance(d, tuple) and d[0] in STRUCTURAL]
    assert structural == [], f"models and migrations disagree: {structural}"


async def test_local_startup_migration_creates_a_ready_database(tmp_path):
    database = tmp_path / "nested" / "agent.db"
    settings = Settings(
        _env_file=None,
        storage_backend="sqlite",
        sqlite_path=database,
    )  # type: ignore[call-arg]

    await migrate(settings)

    assert database.exists()
    engine = create_engine(f"sqlite:///{database}")
    assert set(inspect(engine).get_table_names()) >= {"agent_runs", "users", "approvals"}
    engine.dispose()


async def test_migrated_sqlite_accepts_real_application_writes(tmp_path):
    settings = Settings(
        _env_file=None,
        storage_backend="sqlite",
        sqlite_path=tmp_path / "agent.db",
    )  # type: ignore[call-arg]
    await migrate(settings)
    engine = build_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        run = await create_run(
            session,
            task="billing-service возвращает 5xx после релиза",
            target_service="billing-service",
            actor="local-user",
        )
        assert run.created_at is not None

    await engine.dispose()


async def test_corrupt_local_database_is_rejected_without_overwrite(tmp_path):
    database = tmp_path / "agent.db"
    original = b"not a sqlite database\x00user data"
    database.write_bytes(original)
    settings = Settings(
        _env_file=None,
        storage_backend="sqlite",
        sqlite_path=database,
    )  # type: ignore[call-arg]

    with pytest.raises(DatabaseIntegrityError, match="unreadable"):
        await migrate(settings)

    assert database.read_bytes() == original


async def test_sqlite_accepts_concurrent_local_writes(tmp_path):
    settings = Settings(
        _env_file=None,
        storage_backend="sqlite",
        sqlite_path=tmp_path / "agent.db",
    )  # type: ignore[call-arg]
    engine = build_engine(settings)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE concurrent_writes (value INTEGER)"))

    async def write(value: int) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO concurrent_writes (value) VALUES (:value)"),
                {"value": value},
            )

    await asyncio.gather(*(write(value) for value in range(16)))
    async with engine.connect() as connection:
        count = await connection.scalar(text("SELECT count(*) FROM concurrent_writes"))
        journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
        foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
    await engine.dispose()

    assert count == 16
    assert journal_mode == "wal"
    assert foreign_keys == 1


def test_downgrade_to_base_is_possible(tmp_path):
    """Миграцию, которую нельзя откатить, нельзя безопасно задеплоить."""
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.attributes["sqlalchemy_url"] = f"sqlite+aiosqlite:///{tmp_path / 'down.db'}"
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{tmp_path / 'down.db'}")
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    # Теперь в пустой базе «отсутствует» всё, что определено в моделях.
    dropped = {d[1].name for d in diff if isinstance(d, tuple) and d[0] == "add_table"}
    assert dropped == set(Base.metadata.tables)
