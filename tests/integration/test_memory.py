"""Live pgvector and RLS coverage for long-term memory facts."""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg
import pytest

from core.context import TaskContext
from core.db import service_transaction, user_transaction
from core.embeddings import MockEmbeddingsProvider
from tools.memory import RememberFactArgs, _remember_fact

pytestmark = pytest.mark.integration


async def _create_user(service_pool: asyncpg.Pool, user_id: UUID) -> int:
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
    return chat_id


async def _remove_users(service_pool: asyncpg.Pool, *user_ids: UUID) -> None:
    async with service_transaction(service_pool) as connection:
        await connection.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", list(user_ids))


def _context(user_id: UUID, chat_id: int) -> TaskContext:
    return TaskContext(
        user_id=user_id,
        tg_user_id=chat_id,
        chat_id=chat_id,
        update_id=1,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=uuid4(),
    )


async def test_memory_fact_insert_cosine_dedup_and_rls_isolation(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_a, user_b = uuid4(), uuid4()
    chat_a = await _create_user(service_pool, user_a)
    await _create_user(service_pool, user_b)
    context_a = _context(user_a, chat_a)
    embeddings = MockEmbeddingsProvider()
    try:
        inserted = await _remember_fact(
            app_pool, embeddings, context_a, RememberFactArgs(fact="Предпочитает чёрный кофе")
        )
        deduplicated = await _remember_fact(
            app_pool, embeddings, context_a, RememberFactArgs(fact="Предпочитает чёрный кофе")
        )
        async with user_transaction(app_pool, user_a) as connection:
            own_count = await connection.fetchval("SELECT count(*) FROM memory_facts")
            vector = "[" + ",".join("0" for _ in range(1024)) + "]"
            await connection.fetch(
                "SELECT id FROM memory_facts ORDER BY embedding <=> $1::vector LIMIT 5", vector
            )
        async with user_transaction(app_pool, user_b) as connection:
            foreign_count = await connection.fetchval("SELECT count(*) FROM memory_facts")

        assert inserted.payload == {"deduplicated": False}
        assert deduplicated.payload == {"deduplicated": True}
        assert own_count == 1
        assert foreign_count == 0
    finally:
        await _remove_users(service_pool, user_a, user_b)
