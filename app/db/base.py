"""Декларативная база SQLAlchemy 2 и фабрика асинхронных сессий."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import Settings, get_settings
from app.db.sqlite import configure_engine

# Явные имена нужны, чтобы Alembic стабильно генерировал названия ограничений.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> datetime:
    return datetime.now(UTC)


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings):
    if settings.storage_backend == "sqlite":
        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(settings.database_dsn, pool_pre_ping=True)
    if settings.storage_backend == "sqlite":
        configure_engine(engine)
    return engine


def get_engine():
    global _engine
    if _engine is None:
        _engine = build_engine(get_settings())
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: одна сессия на запрос в границах транзакции."""
    async with get_sessionmaker()() as session:
        yield session
