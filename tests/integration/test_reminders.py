"""Live Postgres checks for reminder RLS boundaries and scheduler delivery."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.context import TaskContext
from core.db import service_transaction, user_transaction
from core.envelope import UpdateEnvelope
from core.queue import DEFAULT_REDIS_QUEUE_URL, enqueue, partition_for_user, stream_key
from tools.reminders import (
    CancelReminderArgs,
    CreateReminderArgs,
    _cancel_reminder,
    _create_reminder,
    _list_reminders,
)
from tools.scheduled import (
    CancelScheduledTaskArgs,
    ScheduleTaskArgs,
    _cancel_scheduled_task,
    _list_scheduled_tasks,
    _schedule_task,
)
from worker.scheduler import SchedulerProcessor, agent_task_update_id

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


async def _queue_redis_or_skip() -> Redis:
    redis: Redis = Redis.from_url(os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL))
    try:
        await redis.ping()
    except (OSError, RedisError) as exc:
        await redis.aclose()
        pytest.skip(f"local dev redis is not reachable: {exc}")
    return redis


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


async def test_scheduled_task_tools_use_app_rls_for_create_list_and_cancel(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> None:
    user_a, user_b = uuid4(), uuid4()
    chat_a = await _create_user(service_pool, user_a)
    chat_b = await _create_user(service_pool, user_b)
    context_a, context_b = _context(user_a, chat_a), _context(user_b, chat_b)
    try:
        created = await _schedule_task(
            app_pool,
            context_a,
            ScheduleTaskArgs(prompt="Подготовь сводку", cron="0 9 * * *", title="Сводка"),
        )
        own = await _list_scheduled_tasks(app_pool, context_a)
        foreign = await _list_scheduled_tasks(app_pool, context_b)
        denied = await _cancel_scheduled_task(
            app_pool,
            context_b,
            CancelScheduledTaskArgs(task_id=int(created.payload["id"])),
        )
        cancelled = await _cancel_scheduled_task(
            app_pool,
            context_a,
            CancelScheduledTaskArgs(task_id=int(created.payload["id"])),
        )

        assert created.status == cancelled.status == "ok"
        assert own.payload["tasks"][0]["id"] == created.payload["id"]
        assert own.payload["tasks"][0]["prompt"] == "Подготовь сводку"
        assert foreign.payload["tasks"] == []
        assert denied.status == "error"
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


async def test_live_scheduler_enqueues_due_agent_task_to_background(
    service_pool: asyncpg.Pool,
) -> None:
    redis = await _queue_redis_or_skip()
    user_id = uuid4()
    chat_id = await _create_user(service_pool, user_id)
    entry_id: str | None = None
    try:
        async with service_transaction(service_pool) as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO scheduled_tasks (
                    user_id, kind, title, payload, cron_expr, next_run_at, status, created_at
                )
                VALUES ($1, 'agent_task', 'Сводка', 'Подготовь сводку', '0 9 * * *',
                        now() - interval '1 minute', 'active', now())
                RETURNING id, next_run_at
                """,
                user_id,
            )
        assert row is not None
        task_id = int(row["id"])
        original_run = row["next_run_at"]
        assert isinstance(original_run, datetime)

        async def sender(_chat_id: int, _text: str) -> None:
            return None

        async def enqueue_background(envelope: UpdateEnvelope) -> str:
            nonlocal entry_id
            entry_id = await enqueue(redis, "background", envelope)
            return entry_id

        assert await SchedulerProcessor(
            service_pool,
            send_reply=sender,
            enqueue_background=enqueue_background,
        ).run_once()
        assert entry_id is not None
        key = stream_key("background", partition_for_user(chat_id))
        entries = await redis.xrange(key, min=entry_id, max=entry_id)
        envelope = UpdateEnvelope.from_stream_entry(entries[0][1])
        assert envelope.kind == "agent_task"
        assert envelope.update_id == agent_task_update_id(task_id, original_run)
        assert envelope.payload == {
            "text": "Подготовь сводку",
            "scheduled_task_id": task_id,
            "title": "Сводка",
        }
        async with service_transaction(service_pool) as connection:
            next_run_at = await connection.fetchval(
                "SELECT next_run_at FROM scheduled_tasks WHERE id = $1", task_id
            )
        assert isinstance(next_run_at, datetime)
        assert next_run_at > datetime.now(UTC)
    finally:
        if entry_id is not None:
            await redis.xdel(stream_key("background", partition_for_user(chat_id)), entry_id)
        await redis.aclose()
        await _remove_users(service_pool, user_id)
