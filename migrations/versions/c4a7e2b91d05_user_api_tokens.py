"""Store a SHA-256 digest of each user's API token.

Nullable because a user may exist without a credential — revoking a token
clears the digest rather than deleting the person, so the audit trail keeps
pointing at someone real.

Revision ID: c4a7e2b91d05
Revises: 91601f7696ff
Create Date: 2026-09-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c4a7e2b91d05"
down_revision: str | None = "91601f7696ff"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("api_token_hash", sa.String(length=64), nullable=True))
    # Уникальность не позволяет одному хешу аутентифицировать двух пользователей,
    # а индекс нужен, потому что каждый запрос ищет токен по этому полю.
    op.create_index("ix_users_api_token_hash", "users", ["api_token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_api_token_hash", table_name="users")
    op.drop_column("users", "api_token_hash")
