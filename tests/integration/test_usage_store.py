"""Integration coverage for RLS-scoped LLM usage persistence."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg
import pytest

from core.db import service_transaction, user_transaction
from core.usage_recorder import UsageRecord
from core.usage_store import save_usage

pytestmark = pytest.mark.integration


async def _create_user(service_pool: asyncpg.Pool, user_id: UUID) -> None:
    async with service_transaction(service_pool) as connection:
        await connection.execute(
            """
            INSERT INTO users (id, tg_user_id, tg_chat_id, status, timezone, created_at, updated_at)
            VALUES ($1, $2, $2, 'active', 'Europe/Moscow', now(), now())
            """,
            user_id,
            int(user_id.int % 1_000_000_000),
        )


async def _delete_usage_and_users(service_pool: asyncpg.Pool, *user_ids: UUID) -> None:
    async with service_transaction(service_pool) as connection:
        await connection.execute(
            "DELETE FROM usage WHERE user_id = ANY($1::uuid[])", list(user_ids)
        )
        await connection.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", list(user_ids))


async def _usage_rows(app_pool: asyncpg.Pool, user_id: UUID) -> list[asyncpg.Record]:
    async with user_transaction(app_pool, user_id) as connection:
        return await connection.fetch(
            """
            SELECT user_id, trace_id, model, prompt_tokens, completion_tokens,
                   cached_tokens, cost_usd, queue
            FROM usage
            ORDER BY id
            """
        )


async def test_save_usage_inserts_records_and_enforces_rls_isolation(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_a = uuid4()
    user_b = uuid4()
    trace_id = uuid4()
    records = [
        UsageRecord("model-a", 10, 4, 2, Decimal("0.001250")),
        UsageRecord("model-b", 11, 5, 0, Decimal(0)),
    ]
    await _create_user(service_pool, user_a)
    await _create_user(service_pool, user_b)
    try:
        await save_usage(app_pool, user_a, trace_id, "interactive", records)
        rows_a = await _usage_rows(app_pool, user_a)
        rows_b = await _usage_rows(app_pool, user_b)
    finally:
        await _delete_usage_and_users(service_pool, user_a, user_b)

    assert [(row["model"], row["cost_usd"], row["queue"]) for row in rows_a] == [
        ("model-a", Decimal("0.001250"), "interactive"),
        ("model-b", Decimal(0), "interactive"),
    ]
    assert all(row["user_id"] == user_a and row["trace_id"] == trace_id for row in rows_a)
    assert rows_b == []
