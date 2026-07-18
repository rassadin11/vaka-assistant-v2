"""Add the optional assistant persona profile."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0003"
down_revision: str | None = "20260717_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a nullable JSONB profile without changing existing rows."""

    op.add_column(
        "users",
        sa.Column("assistant_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Remove only the optional assistant profile."""

    op.drop_column("users", "assistant_profile")
