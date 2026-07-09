"""RLS behavior tests for application and service database roles."""

from uuid import uuid4

import asyncpg
import pytest

from core.db import service_transaction, user_transaction

pytestmark = pytest.mark.integration

TEST_TABLE_NAME = "rls_helper_smoke"


async def test_user_transaction_sets_rls_user_and_service_bypasses_rls(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
    migrator_connection: asyncpg.Connection,
) -> None:
    user_a = uuid4()
    user_b = uuid4()

    await migrator_connection.execute(f'DROP TABLE IF EXISTS "{TEST_TABLE_NAME}"')
    try:
        await migrator_connection.execute(
            f"""
            CREATE TABLE "{TEST_TABLE_NAME}" (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id uuid NOT NULL,
                payload text NOT NULL
            )
            """
        )
        await migrator_connection.execute(
            f'ALTER TABLE "{TEST_TABLE_NAME}" ENABLE ROW LEVEL SECURITY'
        )
        await migrator_connection.execute(
            f"""
            CREATE POLICY "{TEST_TABLE_NAME}_user_isolation"
                ON "{TEST_TABLE_NAME}"
                USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
            """
        )
        await migrator_connection.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{TEST_TABLE_NAME}" TO app, service'
        )
        await migrator_connection.executemany(
            f'INSERT INTO "{TEST_TABLE_NAME}" (user_id, payload) VALUES ($1, $2)',
            [
                (user_a, "a-1"),
                (user_a, "a-2"),
                (user_b, "b-1"),
            ],
        )

        async with user_transaction(app_pool, user_a) as connection:
            visible_payloads = await connection.fetch(
                f'SELECT payload FROM "{TEST_TABLE_NAME}" ORDER BY payload'
            )

        async with app_pool.acquire() as connection:
            without_user_id = await connection.fetchval(f'SELECT count(*) FROM "{TEST_TABLE_NAME}"')

        async with service_transaction(service_pool) as connection:
            service_visible = await connection.fetchval(f'SELECT count(*) FROM "{TEST_TABLE_NAME}"')
    finally:
        await migrator_connection.execute(f'DROP TABLE IF EXISTS "{TEST_TABLE_NAME}"')

    assert [row["payload"] for row in visible_payloads] == ["a-1", "a-2"]
    assert without_user_id == 0
    assert service_visible == 3
