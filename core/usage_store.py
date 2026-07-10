"""RLS-scoped persistence for per-request LLM usage."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import asyncpg

from core.db import user_transaction
from core.queue import QueueName
from core.usage_recorder import UsageRecord


async def save_usage(
    pool: asyncpg.Pool,
    user_id: UUID,
    trace_id: UUID,
    queue: QueueName,
    records: Sequence[UsageRecord],
) -> None:
    """Insert collected usage records in one user-scoped transaction."""

    if not records:
        return
    async with user_transaction(pool, user_id) as connection:
        for record in records:
            await connection.execute(
                """
                INSERT INTO usage (
                    user_id, trace_id, model, prompt_tokens, completion_tokens,
                    cached_tokens, cost_usd, queue, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                """,
                user_id,
                trace_id,
                record.model,
                record.prompt_tokens,
                record.completion_tokens,
                record.cached_tokens,
                record.cost_usd,
                queue,
            )
