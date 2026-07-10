"""Live Redis/Postgres checks for the stage-4 tool registry boundaries."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import asyncpg
import pytest
import uuid_utils
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.context import TaskContext
from core.db import service_transaction, user_transaction
from core.queue import DEFAULT_REDIS_QUEUE_URL
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

pytestmark = pytest.mark.integration


class Args(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str


async def _redis_or_skip(url: str) -> Redis:
    client: Redis = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except (OSError, RedisError) as exc:
        await client.aclose()
        pytest.skip(f"local dev redis is not reachable: {exc}")
    return client


@pytest.fixture
async def queue_redis() -> AsyncIterator[Redis]:
    client = await _redis_or_skip(os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL))
    try:
        yield client
    finally:
        await client.aclose()


async def _create_user(service_pool: asyncpg.Pool, user_id: UUID) -> None:
    async with service_transaction(service_pool) as connection:
        await connection.execute(
            """
            INSERT INTO users (
                id, tg_user_id, tg_chat_id, status, timezone, plan, created_at, updated_at
            )
            VALUES ($1, $2, $2, 'active', 'Europe/Moscow', 'trial', now(), now())
            """,
            user_id,
            int(user_id.int % 1_000_000_000),
        )


async def _remove_users(service_pool: asyncpg.Pool, *user_ids: UUID) -> None:
    async with service_transaction(service_pool) as connection:
        await connection.execute(
            "DELETE FROM tool_calls_log WHERE user_id = ANY($1::uuid[])", list(user_ids)
        )
        await connection.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", list(user_ids))


def _context(user_id: UUID, *, update_id: int = 700) -> TaskContext:
    return TaskContext(
        user_id=user_id,
        tg_user_id=100,
        chat_id=100,
        update_id=update_id,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=uuid4(),
    )


async def test_live_redis_idempotency_and_pending_confirmation_flow(
    queue_redis: Redis,
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_id = uuid4()
    calls = 0
    sent: list[str] = []

    async def handler(_context: TaskContext, args: BaseModel) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(status="ok", payload={"text": args.model_dump()["text"]})

    async def sender(_chat: int, _text: str, buttons: list[list[tuple[str, str]]]) -> None:
        sent.append(buttons[0][0][1])

    registry = ToolRegistry(queue_redis, app_pool, send_confirmation=sender)
    registry.register(
        ToolSpec("mutate", "Mutate test data.", Args, RiskLevel.MUTATING_INTERNAL, handler)
    )
    registry.register(
        ToolSpec("external", "Confirm test data.", Args, RiskLevel.MUTATING_EXTERNAL, handler)
    )
    context = _context(user_id)
    await _create_user(service_pool, user_id)
    try:
        first = await registry.dispatch(context, "mutate", {"text": "one"}, 1)
        second = await registry.dispatch(context, "mutate", {"text": "one"}, 1)
        pending = await registry.dispatch(context, "external", {"text": "two"}, 2)
        confirmation_id = pending.payload["confirmation_id"]
        pending_key = f"pending:{user_id}:{confirmation_id}"

        assert first == second
        assert calls == 1
        assert pending.status == "pending_confirmation"
        assert await queue_redis.get(pending_key) is not None
        assert sent == [f"confirm:{confirmation_id}"]
    finally:
        await queue_redis.delete(
            "idem:700:1",
            "idem:700:2",
            pending_key if "pending_key" in locals() else "unused",
        )
        await _remove_users(service_pool, user_id)


async def test_tool_log_rls_and_service_outbox_access(
    queue_redis: Redis,
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_a = uuid4()
    user_b = uuid4()

    async def handler(_context: TaskContext, _args: BaseModel) -> ToolResult:
        return ToolResult(status="ok")

    registry = ToolRegistry(queue_redis, app_pool)
    registry.register(ToolSpec("read", "Read test data.", Args, RiskLevel.READ_ONLY, handler))
    await _create_user(service_pool, user_a)
    await _create_user(service_pool, user_b)
    action_id = UUID(str(uuid_utils.uuid7()))
    try:
        await registry.dispatch(_context(user_a), "read", {"text": "audit"}, 1)
        async with user_transaction(app_pool, user_a) as connection:
            own_logs = await connection.fetch(
                "SELECT user_id FROM tool_calls_log WHERE tool_name = 'read'"
            )
            await connection.execute(
                """
                INSERT INTO outbox_actions (id, user_id, action, status, attempts, created_at)
                VALUES ($1, $2, '{}'::jsonb, 'pending', 0, now())
                """,
                action_id,
                user_a,
            )
        async with user_transaction(app_pool, user_b) as connection:
            other_logs = await connection.fetch(
                "SELECT user_id FROM tool_calls_log WHERE tool_name = 'read'"
            )
        async with service_transaction(service_pool) as connection:
            outbox = await connection.fetchrow(
                "SELECT user_id FROM outbox_actions WHERE id = $1", action_id
            )

        assert [row["user_id"] for row in own_logs] == [user_a]
        assert other_logs == []
        assert outbox is not None and outbox["user_id"] == user_a
    finally:
        await _remove_users(service_pool, user_a, user_b)
