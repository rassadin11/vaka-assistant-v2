"""Live Postgres checks for reminder RLS boundaries and scheduler delivery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from core.context import TaskContext
from core.db import service_transaction, user_transaction
from tools.reminders import (
    CancelReminderArgs,
    CreateReminderArgs,
    _cancel_reminder,
    _create_reminder,
    _list_reminders,
)
from worker.scheduler import SchedulerProcessor

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


async def test_reminder_tools_use_app_rls_for_create_list_and_cancel(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_a, user_b = uuid4(), uuid4()
    chat_a = await _create_user(service_pool, user_a)
    chat_b = await _create_user(service_pool, user_b)
    context_a, context_b = _context(user_a, chat_a), _context(user_b, chat_b)
    remind_at = (datetime.now(UTC) + timedelta(days=1)).astimezone(ZoneInfo("Europe/Moscow"))
    try:
        created = await _create_reminder(
            app_pool,
            context_a,
            CreateReminderArgs(text="Купить хлеб", remind_at=remind_at.isoformat(), repeat="daily"),
        )
        own = await _list_reminders(app_pool, context_a)
        foreign = await _list_reminders(app_pool, context_b)
        denied = await _cancel_reminder(
            app_pool,
            context_b,
            CancelReminderArgs(reminder_id=int(created.payload["id"])),
        )
        cancelled = await _cancel_reminder(
            app_pool,
            context_a,
            CancelReminderArgs(reminder_id=int(created.payload["id"])),
        )

        assert created.status == cancelled.status == "ok"
        assert own.payload["reminders"] == [
            {
                "id": created.payload["id"],
                "text": "Купить хлеб",
                "next_run_at": remind_at.isoformat(),
                "repeat": "daily",
            }
        ]
        assert foreign.payload["reminders"] == []
        assert denied.status == "error"
        async with user_transaction(app_pool, user_a) as connection:
            active_count = await connection.fetchval(
                "SELECT count(*) FROM scheduled_tasks WHERE status = 'active'"
            )
        assert active_count == 0
    finally:
        await _remove_users(service_pool, user_a, user_b)


async def test_live_scheduler_delivers_rolls_back_failure_and_reschedules(
    service_pool: asyncpg.Pool,
) -> None:
    user_id = uuid4()
    chat_id = await _create_user(service_pool, user_id)
    try:
        async with service_transaction(service_pool) as connection:
            one_off_id = await connection.fetchval(
                """
                INSERT INTO scheduled_tasks (
                    user_id, kind, title, payload, cron_expr, next_run_at, status, created_at
                )
                VALUES (
                    $1, 'reminder', 'one', 'one', NULL,
                    now() - interval '1 minute', 'active', now()
                )
                RETURNING id
                """,
                user_id,
            )
        sent: list[tuple[int, str]] = []

        async def sender(sent_chat_id: int, text: str) -> None:
            sent.append((sent_chat_id, text))

        assert await SchedulerProcessor(service_pool, send_reply=sender).run_once()
        assert sent == [(chat_id, "Напоминание: one")]
        async with service_transaction(service_pool) as connection:
            one_off_status = await connection.fetchval(
                "SELECT status FROM scheduled_tasks WHERE id = $1", one_off_id
            )
            failed_id = await connection.fetchval(
                """
                INSERT INTO scheduled_tasks (
                    user_id, kind, title, payload, cron_expr, next_run_at, status, created_at
                )
                VALUES (
                    $1, 'reminder', 'failed', 'failed', NULL,
                    now() - interval '1 minute', 'active', now()
                )
                RETURNING id
                """,
                user_id,
            )
        assert one_off_status == "done"

        async def failing_sender(_chat_id: int, _text: str) -> None:
            raise RuntimeError("telegram unavailable")

        with pytest.raises(RuntimeError, match="telegram unavailable"):
            await SchedulerProcessor(service_pool, send_reply=failing_sender).run_once()
        async with service_transaction(service_pool) as connection:
            failed_status = await connection.fetchval(
                "SELECT status FROM scheduled_tasks WHERE id = $1", failed_id
            )
            recurring_id = await connection.fetchval(
                """
                INSERT INTO scheduled_tasks (
                    user_id, kind, title, payload, cron_expr, next_run_at, status, created_at
                )
                VALUES (
                    $1, 'reminder', 'weekly', 'weekly', '30 23 * * 6',
                    now() - interval '1 minute', 'active', now()
                )
                RETURNING id
                """,
                user_id,
            )
        assert failed_status == "active"

        async def selective_sender(_chat_id: int, text: str) -> None:
            if text == "Напоминание: failed":
                raise RuntimeError("unexpected retry")

        # Mark the deliberately failed row done so the recurring row is the only due delivery.
        async with service_transaction(service_pool) as connection:
            await connection.execute(
                "UPDATE scheduled_tasks SET status = 'done' WHERE id = $1", failed_id
            )
        assert await SchedulerProcessor(service_pool, send_reply=selective_sender).run_once()
        async with service_transaction(service_pool) as connection:
            next_run_at = await connection.fetchval(
                "SELECT next_run_at FROM scheduled_tasks WHERE id = $1", recurring_id
            )
        assert isinstance(next_run_at, datetime)
        assert next_run_at > datetime.now(UTC)
        local_next = next_run_at.astimezone(ZoneInfo("Europe/Moscow"))
        assert (local_next.hour, local_next.minute, local_next.weekday()) == (23, 30, 5)
    finally:
        await _remove_users(service_pool, user_id)
