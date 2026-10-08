"""Фиксирует, был ли вызов инструмента обслужен из кэша.

Автогенерация выполнялась против SQLite и дополнительно предложила пакет
изменений типов NUMERIC → UUID. Это артефакт того, что в SQLite нет
встроенного типа UUID: в PostgreSQL столбцы уже ``uuid``, поэтому такие
изменения были бы шумом в лучшем случае и перезаписью таблицы в худшем.
Оставлен только новый столбец.

Revision ID: 91601f7696ff
Revises: 46524fabd6fa
Create Date: 2026-09-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "91601f7696ff"
down_revision: str | None = "46524fabd6fa"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # server_default позволяет добавить NOT NULL в таблицу с существующими строками:
    # старые вызовы появились до кэширования и не могли быть обслужены из кэша.
    # Значение по умолчанию сохраняется, потому что SQLite не умеет изменять его,
    # а тесты выполняют те же миграции в SQLite. Значение false ничего не стоит,
    # поскольку приложение всегда явно передаёт этот флаг.
    op.add_column(
        "tool_calls",
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tool_calls", "cached")
