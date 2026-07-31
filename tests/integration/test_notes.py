"""Live coverage for note tools against PostgreSQL row level security."""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg
import pytest

from core.context import TaskContext
from core.db import service_transaction, user_transaction
from tools.notes import (
    AppendToNoteArgs,
    ReadNoteArgs,
    UpsertNoteArgs,
    _append_to_note,
    _list_notes,
    _read_note,
    _upsert_note,
)

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


async def test_notes_upsert_append_and_rls_isolation(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_a, user_b = uuid4(), uuid4()
    chat_a = await _create_user(service_pool, user_a)
    chat_b = await _create_user(service_pool, user_b)
    context_a = _context(user_a, chat_a)
    context_b = _context(user_b, chat_b)
    try:
        created = await _upsert_note(
            app_pool, context_a, UpsertNoteArgs(title="Пробежки", content="2026-07-30: 5 км")
        )
        appended = await _append_to_note(
            app_pool, context_a, AppendToNoteArgs(title="пробежки", text="2026-07-31: 6 км")
        )
        own = await _read_note(app_pool, context_a, ReadNoteArgs(title="Пробежки"))
        foreign = await _read_note(app_pool, context_b, ReadNoteArgs(title="Пробежки"))
        foreign_list = await _list_notes(app_pool, context_b)

        same_title_other_user = await _upsert_note(
            app_pool, context_b, UpsertNoteArgs(title="Пробежки", content="свои записи")
        )

        async with user_transaction(app_pool, user_a) as connection:
            own_count = await connection.fetchval("SELECT count(*) FROM notes")

        assert created.payload["created"] is True
        assert appended.payload["created"] is False
        assert own.payload["content"] == "2026-07-30: 5 км\n2026-07-31: 6 км"
        assert foreign.status == "error"
        assert foreign_list.payload["notes"] == []
        assert same_title_other_user.payload == {
            "created": True,
            "notes_count": 1,
            "notes_limit": 100,
        }
        assert own_count == 1
    finally:
        await _remove_users(service_pool, user_a, user_b)
