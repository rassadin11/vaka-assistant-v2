"""Add topic notes with row level security."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260731_0004"
down_revision: str | None = "20260718_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the notes table; existing code keeps working untouched."""

    op.execute(
        """
        CREATE TABLE notes (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title text NOT NULL,
            content text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, title)
        )
        """
    )
    op.execute("CREATE INDEX notes_user_id_updated_at_idx ON notes (user_id, updated_at DESC)")

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON notes TO app, service")
    op.execute("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO app, service")

    op.execute("ALTER TABLE notes ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY notes_user_isolation
            ON notes
            USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    """Drop the notes table and everything created with it."""

    op.execute("DROP TABLE IF EXISTS notes CASCADE")
