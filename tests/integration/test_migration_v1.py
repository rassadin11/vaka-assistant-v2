"""Integration checks for the v1 Alembic schema migration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from core.db import user_transaction

from .db_support import MIGRATOR_DATABASE_URL

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
MIGRATIONS_DATABASE_URL = MIGRATOR_DATABASE_URL.replace(
    "postgresql://",
    "postgresql+psycopg://",
    1,
)

EXPECTED_TABLES = {
    "users",
    "messages",
    "dialog_summaries",
    "memory_facts",
    "transactions",
    "budgets",
    "scheduled_tasks",
    "documents",
    "doc_chunks",
    "oauth_tokens",
    "outbox_actions",
    "tool_calls_log",
    "usage",
}

PARTITIONED_TABLES = {"tool_calls_log", "usage"}


def _run_alembic_upgrade() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")


async def test_v1_migration_schema_rls_partitioning_and_app_smoke(
    monkeypatch: pytest.MonkeyPatch,
    postgres_roles: None,
    app_pool: asyncpg.Pool,
    migrator_connection: asyncpg.Connection,
) -> None:
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", MIGRATIONS_DATABASE_URL)
    await asyncio.to_thread(_run_alembic_upgrade)

    await _assert_expected_tables(migrator_connection)
    await _assert_assistant_profile_column(migrator_connection)
    await _assert_rls_policies(migrator_connection)
    await _assert_partitions(migrator_connection)
    await _assert_app_role_rls_smoke(app_pool, migrator_connection)


async def _assert_expected_tables(connection: asyncpg.Connection) -> None:
    rows = await connection.fetch(
        """
        SELECT c.relname
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relname = ANY($1::text[])
        """,
        list(EXPECTED_TABLES),
    )

    assert {row["relname"] for row in rows} == EXPECTED_TABLES


async def _assert_assistant_profile_column(connection: asyncpg.Connection) -> None:
    row = await connection.fetchrow(
        """
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'users'
          AND column_name = 'assistant_profile'
        """
    )

    assert row is not None
    assert row["data_type"] == "jsonb"
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None


async def _assert_rls_policies(connection: asyncpg.Connection) -> None:
    user_id_tables = await connection.fetch(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND column_name = 'user_id'
          AND table_name = ANY($1::text[])
        """,
        list(EXPECTED_TABLES),
    )
    rls_tables = {row["table_name"] for row in user_id_tables} | {"users"}

    rows = await connection.fetch(
        """
        SELECT
            c.relname,
            c.relrowsecurity,
            count(p.polname)::int AS policy_count
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        LEFT JOIN pg_policy AS p ON p.polrelid = c.oid
        WHERE n.nspname = 'public'
          AND c.relname = ANY($1::text[])
        GROUP BY c.relname, c.relrowsecurity
        """,
        list(rls_tables),
    )

    by_table = {row["relname"]: row for row in rows}
    assert set(by_table) == rls_tables
    for table, row in by_table.items():
        assert row["relrowsecurity"], f"{table} has RLS disabled"
        assert row["policy_count"] >= 1, f"{table} has no RLS policy"


async def _assert_partitions(connection: asyncpg.Connection) -> None:
    rows = await connection.fetch(
        """
        SELECT
            parent.relname,
            parent.relkind::text AS relkind,
            count(child.oid)::int AS child_count
        FROM pg_class AS parent
        JOIN pg_namespace AS n ON n.oid = parent.relnamespace
        LEFT JOIN pg_inherits AS i ON i.inhparent = parent.oid
        LEFT JOIN pg_class AS child ON child.oid = i.inhrelid
        WHERE n.nspname = 'public'
          AND parent.relname = ANY($1::text[])
        GROUP BY parent.relname, parent.relkind
        """,
        list(PARTITIONED_TABLES),
    )

    by_table = {row["relname"]: row for row in rows}
    assert set(by_table) == PARTITIONED_TABLES
    for table, row in by_table.items():
        assert row["relkind"] == "p", f"{table} is not a partitioned table"
        assert row["child_count"] >= 1, f"{table} has no child partitions"


async def _assert_app_role_rls_smoke(
    app_pool: asyncpg.Pool,
    migrator_connection: asyncpg.Connection,
) -> None:
    user_a = uuid4()
    user_b = uuid4()
    message_id = uuid4()
    tg_user_id = -int(user_a.hex[:12], 16)

    try:
        async with user_transaction(app_pool, user_a) as connection:
            await connection.execute(
                """
                INSERT INTO users (id, tg_user_id, tg_chat_id, timezone)
                VALUES ($1, $2, $2, 'Europe/Moscow')
                """,
                user_a,
                tg_user_id,
            )
            await connection.execute(
                """
                INSERT INTO messages (id, user_id, role, content)
                VALUES ($1, $2, 'user', 'migration smoke')
                """,
                message_id,
                user_a,
            )
            own_count = await connection.fetchval(
                "SELECT count(*) FROM messages WHERE id = $1",
                message_id,
            )

        async with user_transaction(app_pool, user_b) as connection:
            other_count = await connection.fetchval(
                "SELECT count(*) FROM messages WHERE id = $1",
                message_id,
            )
    finally:
        await migrator_connection.execute(
            "DELETE FROM users WHERE id = ANY($1::uuid[])",
            [user_a, user_b],
        )

    assert own_count == 1
    assert other_count == 0
