"""Long-term user facts, searched by pgvector or by Postgres full text search."""

from __future__ import annotations

from typing import cast
from uuid import UUID

import asyncpg
import uuid_utils
from asyncpg.pool import PoolConnectionProxy
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.db import user_transaction
from core.embeddings import EMBEDDING_DIMENSIONS, EmbeddingsProvider, EmbeddingsUnavailableError
from core.profile import RetrievalBackend, profile_limits
from core.retrieval import find_duplicate_fact_fts
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

DEDUPLICATION_THRESHOLD = 0.92


class RememberFactArgs(BaseModel):
    """The single atomic fact supplied by the model."""

    model_config = ConfigDict(extra="ignore")

    fact: str = Field(max_length=500)


def register_memory_tools(
    registry: ToolRegistry,
    app_pool: asyncpg.Pool,
    embeddings: EmbeddingsProvider | None,
    *,
    retrieval: RetrievalBackend = RetrievalBackend.VECTOR,
) -> None:
    """Register memory; the caller guarantees the chosen backend is usable."""

    async def remember_fact(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _remember_fact(
            app_pool, embeddings, ctx, cast(RememberFactArgs, args), retrieval=retrieval
        )

    registry.register(
        ToolSpec(
            name="remember_fact",
            description="Store one durable fact about the user for future conversations.",
            args_schema=RememberFactArgs,
            risk=RiskLevel.MUTATING_INTERNAL,
            handler=remember_fact,
            daily_limit=50,
        )
    )


async def _remember_fact(
    pool: asyncpg.Pool,
    embeddings: EmbeddingsProvider | None,
    context: TaskContext,
    args: RememberFactArgs,
    *,
    retrieval: RetrievalBackend = RetrievalBackend.VECTOR,
) -> ToolResult:
    vector_literal: str | None = None
    if retrieval is RetrievalBackend.VECTOR:
        if embeddings is None:
            raise ValueError("the vector backend requires an embeddings provider")
        try:
            vectors = await embeddings.embed([args.fact], "passage")
        except EmbeddingsUnavailableError:
            return ToolResult(
                status="error",
                error="Память временно недоступна. Попробуйте позже.",
                retryable=True,
            )
        vector_literal = vector_to_literal(_single_vector(vectors))

    async with user_transaction(pool, context.user_id) as connection:
        duplicate = (
            await _nearest_vector_duplicate(connection, vector_literal)
            if vector_literal is not None
            else await find_duplicate_fact_fts(connection, args.fact)
        )
        if duplicate is not None:
            await connection.execute(
                "UPDATE memory_facts SET updated_at = now(), last_used_at = now() WHERE id = $1",
                duplicate,
            )
            return ToolResult(status="ok", payload={"deduplicated": True})

        await connection.execute(
            """
            INSERT INTO memory_facts (
                id, user_id, text, embedding, last_used_at, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4::vector, now(), now(), now())
            """,
            UUID(str(uuid_utils.uuid7())),
            context.user_id,
            args.fact,
            vector_literal,
        )
        fact_count = await connection.fetchval("SELECT count(*) FROM memory_facts")
        max_facts = profile_limits().facts_per_user
        if int(fact_count) > max_facts:
            await connection.execute(
                """
                DELETE FROM memory_facts
                WHERE id IN (
                    SELECT id FROM memory_facts
                    ORDER BY last_used_at DESC, id DESC
                    OFFSET $1
                )
                """,
                max_facts,
            )
    return ToolResult(status="ok", payload={"deduplicated": False})


async def _nearest_vector_duplicate(
    connection: PoolConnectionProxy[asyncpg.Record], vector_literal: str
) -> UUID | None:
    nearest = await connection.fetchrow(
        """
        SELECT id, 1 - (embedding <=> $1::vector) AS sim
        FROM memory_facts
        ORDER BY embedding <=> $1::vector
        LIMIT 1
        """,
        vector_literal,
    )
    if nearest is None or float(nearest["sim"]) <= DEDUPLICATION_THRESHOLD:
        return None
    return cast(UUID, nearest["id"])


def vector_to_literal(vector: list[float]) -> str:
    """Serialize a validated vector for pgvector's text input syntax."""

    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"embedding must have {EMBEDDING_DIMENSIONS} dimensions")
    return "[" + ",".join(format(value, ".17g") for value in vector) + "]"


def _single_vector(vectors: list[list[float]]) -> list[float]:
    if len(vectors) != 1:
        raise ValueError("embeddings provider returned an unexpected number of vectors")
    return vectors[0]
