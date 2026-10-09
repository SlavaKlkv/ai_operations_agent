import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Объект конфигурации Alembic предоставляет доступ
# к значениям используемого ini-файла.
config = context.config

# Применить конфигурацию логирования Python.
# Эта строка настраивает логгеры.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Здесь указывается объект MetaData модели
# для поддержки autogenerate.
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
from app.core.config import get_settings
from app.db import models  # noqa: F401 — импорт регистрирует таблицы побочным эффектом
from app.db.base import Base

target_metadata = Base.metadata

# DSN хранится в окружении, а не в alembic.ini. Тесты переопределяют его через
# ``config.attributes``, чтобы выполнять те же миграции в SQLite.
config.set_main_option(
    "sqlalchemy.url",
    config.attributes.get("sqlalchemy_url") or get_settings().database_dsn,
)

# Другие значения конфигурации, необходимые env.py,
# можно получить так:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Запускает миграции в режиме 'offline'.

    Здесь контекст настраивается только по URL,
    а не по Engine, хотя Engine тоже допустим
    в этом месте.  Пропуская создание Engine,
    нам даже не нужен доступный DBAPI.

    Вызовы context.execute() здесь выводят переданную строку
    в вывод скрипта.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """В этом сценарии нужно создать Engine
    и связать соединение с контекстом.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Запускает миграции в режиме 'online'."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
