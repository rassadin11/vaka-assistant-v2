"""Integration checks for the full text search migration (small profile, S3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from core.db import service_transaction, user_transaction

from .db_support import MIGRATOR_DATABASE_URL

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
MIGRATIONS_DATABASE_URL = MIGRATOR_DATABASE_URL.replace(
    "postgresql://",
    "postgresql+psycopg://",
    1,
)
FTS_REVISION = "20260907_0005"
SEARCHABLE_TABLES = {"memory_facts", "doc_chunks"}
ZERO_VECTOR = "[" + ",".join(["0.01"] * 1024) + "]"


def _alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def _run_upgrade(target: str = "head") -> None:
    command.upgrade(_alembic_config(), target)


def _run_downgrade(target: str) -> None:
    command.downgrade(_alembic_config(), target)


async def test_fts_columns_and_indexes_exist(
    monkeypatch: pytest.MonkeyPatch,
    postgres_roles: None,
    migrator_connection: asyncpg.Connection,
) -> None:
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", MIGRATIONS_DATABASE_URL)
    await asyncio.to_thread(_run_upgrade)

    columns = await migrator_connection.fetch(
        """
        SELECT table_name, data_type, is_generated, generation_expression
        FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'text_tsv'
        """
    )

    assert {row["table_name"] for row in columns} == SEARCHABLE_TABLES
    for row in columns:
        assert row["data_type"] == "tsvector"
        # Stored, not virtual: the value has to survive pg_restore --data-only
        # of a production dump (plan S18) without a reindex step.
        assert row["is_generated"] == "ALWAYS"
        assert "'russian'" in row["generation_expression"]

    indexes = await migrator_connection.fetch(
        """
        SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND indexname LIKE '%%text_tsv%%'
        """
    )

    assert len(indexes) == len(SEARCHABLE_TABLES)
    assert all("USING gin" in row["indexdef"] for row in indexes)

    trigram_version = await migrator_connection.fetchval(
        "SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'"
    )
    assert trigram_version is not None


async def test_vector_search_path_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
    postgres_roles: None,
    migrator_connection: asyncpg.Connection,
) -> None:
    """The prod profile keeps its cosine path: same columns, same HNSW indexes."""

    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", MIGRATIONS_DATABASE_URL)
    await asyncio.to_thread(_run_upgrade)

    embeddings = await migrator_connection.fetch(
        """
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'embedding'
        """
    )
    hnsw = await migrator_connection.fetch(
        """
        SELECT indexname FROM pg_indexes
        WHERE schemaname = 'public' AND indexdef LIKE '%%hnsw%%'
        """
    )

    assert {row["table_name"] for row in embeddings} == SEARCHABLE_TABLES
    assert len(hnsw) == len(SEARCHABLE_TABLES)


async def test_generated_column_is_filled_and_searchable(
    monkeypatch: pytest.MonkeyPatch,
    postgres_roles: None,
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    """The insert statement shipping today keeps working and fills text_tsv."""

    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", MIGRATIONS_DATABASE_URL)
    await asyncio.to_thread(_run_upgrade)

    user_id = uuid4()
    await _create_user(service_pool, user_id)
    try:
        async with user_transaction(app_pool, user_id) as connection:
            await connection.execute(
                """
                INSERT INTO memory_facts (
                    id, user_id, text, embedding, last_used_at, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4::vector, now(), now(), now())
                """,
                UUID(str(uuid4())),
                user_id,
                "Ежедневно езжу на работу на велосипеде через набережную",
                ZERO_VECTOR,
            )

        async with user_transaction(app_pool, user_id) as connection:
            hits = await connection.fetch(
                """
                SELECT text, ts_rank(text_tsv, websearch_to_tsquery('russian', $1)) AS rank
                FROM memory_facts
                WHERE text_tsv @@ websearch_to_tsquery('russian', $1)
                """,
                # A different word form than the stored one: the stemmer, not a
                # substring match, is what has to find this row.
                "велосипеде",
            )
            similarity = await connection.fetchval(
                "SELECT max(similarity(text, $1)) FROM memory_facts", "набережная"
            )

        assert [row["text"] for row in hits] == [
            "Ежедневно езжу на работу на велосипеде через набережную"
        ]
        assert float(hits[0]["rank"]) > 0
        assert float(similarity) > 0
    finally:
        await _remove_user(service_pool, user_id)


async def test_generated_column_rejects_writes(
    monkeypatch: pytest.MonkeyPatch,
    postgres_roles: None,
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    """Nothing in the application may set text_tsv; the database owns it."""

    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", MIGRATIONS_DATABASE_URL)
    await asyncio.to_thread(_run_upgrade)

    user_id = uuid4()
    await _create_user(service_pool, user_id)
    try:
        with pytest.raises(asyncpg.PostgresError):
            async with user_transaction(app_pool, user_id) as connection:
                await connection.execute(
                    "UPDATE memory_facts SET text_tsv = to_tsvector('russian', 'x')"
                )
    finally:
        await _remove_user(service_pool, user_id)


async def test_downgrade_removes_only_what_the_migration_added(
    monkeypatch: pytest.MonkeyPatch,
    postgres_roles: None,
    migrator_connection: asyncpg.Connection,
) -> None:
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", MIGRATIONS_DATABASE_URL)
    await asyncio.to_thread(_run_upgrade)
    await asyncio.to_thread(_run_downgrade, f"{FTS_REVISION}-1")
    try:
        remaining_columns = await migrator_connection.fetchval(
            """
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND column_name = 'text_tsv'
            """
        )
        remaining_indexes = await migrator_connection.fetchval(
            """
            SELECT count(*) FROM pg_indexes
            WHERE schemaname = 'public' AND indexname LIKE '%%text_tsv%%'
            """
        )
        hnsw = await migrator_connection.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexdef LIKE '%%hnsw%%'"
        )

        assert remaining_columns == 0
        assert remaining_indexes == 0
        assert hnsw == len(SEARCHABLE_TABLES)
    finally:
        await asyncio.to_thread(_run_upgrade)


async def _create_user(service_pool: asyncpg.Pool, user_id: UUID) -> None:
    chat_id = int(user_id.int % 1_000_000_000)
    async with service_transaction(service_pool) as connection:
        await connection.execute(
            """
            INSERT INTO users (
                id, tg_user_id, tg_chat_id, status, timezone, plan, created_at, updated_at
            )
            VALUES ($1, $2, $2, 'active', 'Europe/Moscow', 'trial', now(), now())
            """,
            user_id,
            chat_id,
        )


async def _remove_user(service_pool: asyncpg.Pool, user_id: UUID) -> None:
    async with service_transaction(service_pool) as connection:
        await connection.execute("DELETE FROM users WHERE id = $1", user_id)
