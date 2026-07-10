"""Integration coverage for RLS-scoped dialogue persistence."""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg
import pytest

from core.db import service_transaction
from core.dialog_store import MessageDraft, load_dialog, save_messages, save_summary
from core.llm import LLMToolCall

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


async def _delete_users(service_pool: asyncpg.Pool, *user_ids: UUID) -> None:
    async with service_transaction(service_pool) as connection:
        await connection.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", list(user_ids))


async def test_save_messages_load_dialog_round_trip_under_app_role(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_id = uuid4()
    trace_id = uuid4()
    await _create_user(service_pool, user_id)
    try:
        ids = await save_messages(
            app_pool,
            user_id,
            [
                MessageDraft(role="user", content="hello", meta={"kind": "text"}),
                MessageDraft(
                    role="assistant",
                    tool_calls=[LLMToolCall(id="call-1", name="tool", arguments_json="{}")],
                ),
                MessageDraft(role="tool", content="result", tool_call_id="call-1"),
            ],
            trace_id,
        )
        history = await load_dialog(app_pool, user_id)
    finally:
        await _delete_users(service_pool, user_id)

    assert [message.id for message in history.tail] == ids
    assert [message.role for message in history.tail] == ["user", "assistant", "tool"]
    assert history.tail[0].meta == {"kind": "text"}
    assert history.tail[1].tool_calls == [
        LLMToolCall(id="call-1", name="tool", arguments_json="{}")
    ]


async def test_app_role_cannot_read_another_users_dialogue(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_a = uuid4()
    user_b = uuid4()
    await _create_user(service_pool, user_a)
    await _create_user(service_pool, user_b)
    try:
        await save_messages(
            app_pool,
            user_a,
            [MessageDraft(role="user", content="private A")],
            uuid4(),
        )
        history_a = await load_dialog(app_pool, user_a)
        history_b = await load_dialog(app_pool, user_b)
    finally:
        await _delete_users(service_pool, user_a, user_b)

    assert [message.content for message in history_a.tail] == ["private A"]
    assert history_b.summary is None
    assert history_b.tail == []


async def test_summary_boundary_returns_only_messages_after_it(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_id = uuid4()
    await _create_user(service_pool, user_id)
    try:
        ids = await save_messages(
            app_pool,
            user_id,
            [
                MessageDraft(role="user", content="first"),
                MessageDraft(role="assistant", content="second"),
                MessageDraft(role="user", content="third"),
            ],
            uuid4(),
        )
        await save_summary(app_pool, user_id, "first two summarized", ids[1], 4)
        history = await load_dialog(app_pool, user_id)
    finally:
        await _delete_users(service_pool, user_id)

    assert history.summary is not None
    assert history.summary.text == "first two summarized"
    assert [message.id for message in history.tail] == [ids[2]]
    assert [message.content for message in history.tail] == ["third"]
