"""Add Russian full text search columns to memory facts and document chunks."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260907_0005"
down_revision: str | None = "20260731_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add generated tsvector columns and their indexes; nothing else changes.

    Additive by construction: every insert in the code names its columns, so a
    generated column is invisible to the running version. The vector columns and
    their HNSW indexes stay exactly as they are -- the prod profile keeps
    searching by cosine distance and never reads what is created here.

    The dictionary is spelled out (``'russian'``) because the single-argument
    ``to_tsvector`` depends on ``default_text_search_config`` and is therefore
    only STABLE; a generated column requires an IMMUTABLE expression.
    """

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        ALTER TABLE memory_facts
            ADD COLUMN text_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED
        """
    )
    op.execute("CREATE INDEX memory_facts_text_tsv_idx ON memory_facts USING gin (text_tsv)")

    op.execute(
        """
        ALTER TABLE doc_chunks
            ADD COLUMN text_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED
        """
    )
    op.execute("CREATE INDEX doc_chunks_text_tsv_idx ON doc_chunks USING gin (text_tsv)")


def downgrade() -> None:
    """Drop the search columns and their indexes.

    ``pg_trgm`` is deliberately left installed: an extension is database-wide
    state that other objects may come to depend on, and keeping it costs
    nothing.
    """

    op.execute("DROP INDEX IF EXISTS doc_chunks_text_tsv_idx")
    op.execute("ALTER TABLE doc_chunks DROP COLUMN IF EXISTS text_tsv")

    op.execute("DROP INDEX IF EXISTS memory_facts_text_tsv_idx")
    op.execute("ALTER TABLE memory_facts DROP COLUMN IF EXISTS text_tsv")
