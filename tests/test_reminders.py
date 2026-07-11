"""Offline coverage for reminder tools and the reminder scheduler."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from core.context import TaskContext
from tools.reminders import (
    CancelReminderArgs,
    CreateReminderArgs,
    _cancel_reminder,
    _create_reminder,
    _list_reminders,
    _repeat_for_cron,
)
from tools.scheduled import (
    CancelScheduledTaskArgs,
    ScheduleTaskArgs,
    _cancel_scheduled_task,
    _list_scheduled_tasks,
    _schedule_task,
)
from worker.scheduler import SchedulerProcessor, agent_task_update_id


class FakeTransaction:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self._snapshot: list[dict[str, Any]] | None = None

    async def __aenter__(self) -> None:
        self._snapshot = deepcopy(self._connection.tasks)
        return None

    async def __aexit__(self, exc_type: object, *args: object) -> None:
        if exc_type is not None and self._snapshot is not None:
            self._connection.tasks = self._snapshot


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self._next_id = 1

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            return "SELECT 1"
        if "SET status = 'cancelled'" in query:
            reminder_id = int(args[0])
            kind = "agent_task" if "agent_task" in query else "reminder"
            for task in self.tasks:
                if (
                    task["id"] == reminder_id
                    and task["kind"] == kind
                    and task["status"] == "active"
                ):
                    task["status"] = "cancelled"
                    return "UPDATE 1"
            return "UPDATE 0"
        if "SET status = 'done'" in query:
            self._task(int(args[0]))["status"] = "done"
            return "UPDATE 1"
        if "SET next_run_at = $2" in query:
            task = self._task(int(args[0]))
            task["next_run_at"] = args[1]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {query}")

    async def fetchval(self, query: str, *args: object) -> object:
        if "COUNT(*)" in query:
            kind = "agent_task" if "agent_task" in query else "reminder"
            return sum(task["kind"] == kind and task["status"] == "active" for task in self.tasks)
        if "INSERT INTO scheduled_tasks" in query:
            kind = "agent_task" if "agent_task" in query else "reminder"
            task = {
                "id": self._next_id,
                "user_id": args[0],
                "kind": kind,
                "title": args[1],
                "payload": args[2],
                "cron_expr": args[3],
                "next_run_at": args[4],
                "status": "active",
                "tg_chat_id": 500,
                "timezone": "Europe/Moscow",
            }
            self.tasks.append(task)
            self._next_id += 1
            return task["id"]
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        del args
        if "JOIN users" in query:
            now = datetime.now(UTC)
            return [
                task.copy()
                for task in self.tasks
                if task["kind"] in {"reminder", "agent_task"}
                and task["status"] == "active"
                and task["next_run_at"] <= now
            ][:20]
        if "FROM scheduled_tasks" in query:
            kind = "agent_task" if "agent_task" in query else "reminder"
            return [
                task.copy()
                for task in sorted(self.tasks, key=lambda value: value["next_run_at"])
                if task["kind"] == kind and task["status"] == "active"
            ][: 10 if kind == "agent_task" else 25]
        raise AssertionError(f"unexpected fetch: {query}")

    def _task(self, task_id: int) -> dict[str, Any]:
        return next(task for task in self.tasks if task["id"] == task_id)


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        update_id=101,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


async def test_create_reminder_uses_local_time_for_cron_and_utc_storage() -> None:
    pool = FakePool()
    result = await _create_reminder(
        pool,
        _context(),
        CreateReminderArgs(text="Позвонить", remind_at="2027-07-10T23:30:00", repeat="weekly"),
    )

    row = pool.connection.tasks[0]
    assert result.status == "ok"
    assert row["cron_expr"] == "30 23 * * 6"
    assert row["next_run_at"] == datetime(2027, 7, 10, 20, 30, tzinfo=UTC)
    assert result.payload["next_run_at"] == "2027-07-10T23:30:00+03:00"


async def test_create_reminder_interprets_naive_time_and_truncates_title() -> None:
    pool = FakePool()
    local_future = (datetime.now(UTC) + timedelta(days=2)).astimezone().replace(tzinfo=None)
    text = "x" * 81
    result = await _create_reminder(
        pool,
        _context(),
        CreateReminderArgs(text=text, remind_at=local_future.isoformat(), repeat="monthly"),
    )

    row = pool.connection.tasks[0]
    assert result.status == "ok"
    assert row["title"] == "x" * 80
    assert row["next_run_at"].utcoffset() == timedelta(0)
    assert row["cron_expr"] == f"{local_future.minute} {local_future.hour} {local_future.day} * *"


async def test_create_reminder_rejects_past_with_current_local_time() -> None:
    result = await _create_reminder(
        FakePool(),
        _context(),
        CreateReminderArgs(text="Просрочено", remind_at="2020-01-01T10:00:00"),
    )

    assert result.status == "error"
    assert result.retryable is True
    assert "Сейчас по вашему времени:" in str(result.error)


async def test_active_limit_cancel_and_list_reverse_cron_mapping() -> None:
    pool = FakePool()
    future = datetime.now(UTC) + timedelta(days=1)
    pool.connection.tasks = [
        {
            "id": index + 1,
            "kind": "reminder",
            "status": "active",
            "payload": f"r{index}",
            "cron_expr": None,
            "next_run_at": future + timedelta(minutes=index),
        }
        for index in range(25)
    ]
    limited = await _create_reminder(
        pool,
        _context(),
        CreateReminderArgs(text="one more", remind_at=(future + timedelta(days=1)).isoformat()),
    )
    cancelled = await _cancel_reminder(pool, _context(), CancelReminderArgs(reminder_id=1))
    missing = await _cancel_reminder(pool, _context(), CancelReminderArgs(reminder_id=999))
    pool.connection.tasks[1]["cron_expr"] = "30 23 * * *"
    pool.connection.tasks[2]["cron_expr"] = "30 23 * * 6"
    pool.connection.tasks[3]["cron_expr"] = "30 23 11 * *"
    pool.connection.tasks[4]["cron_expr"] = "*/5 * * * *"
    listed = await _list_reminders(pool, _context())

    assert limited.status == "error"
    assert limited.retryable is False
    assert cancelled.status == "ok"
    assert missing.status == "error"
    assert listed.payload["reminders"][0]["repeat"] == "daily"
    assert [row["repeat"] for row in listed.payload["reminders"][:4]] == [
        "daily",
        "weekly",
        "monthly",
        "cron",
    ]
    assert _repeat_for_cron("0 9 * * MON") == "cron"


async def test_scheduler_delivers_one_off_and_enqueues_agent_tasks() -> None:
    pool = FakePool()
    pool.connection.tasks = [
        _task(1, "reminder", None),
        _task(2, "agent_task", "0 9 * * *"),
    ]
    agent_next_run = pool.connection.tasks[1]["next_run_at"]
    sent: list[str] = []

    async def sender(_chat_id: int, text: str) -> None:
        sent.append(text)

    enqueued = []

    async def enqueue_background(envelope: object) -> None:
        enqueued.append(envelope)

    worked = await SchedulerProcessor(
        pool, send_reply=sender, enqueue_background=enqueue_background
    ).run_once()

    assert worked is True
    assert sent == ["Напоминание: r1"]
    assert pool.connection.tasks[0]["status"] == "done"
    assert pool.connection.tasks[1]["status"] == "active"
    envelope = enqueued[0]
    assert envelope.kind == "agent_task"
    assert envelope.payload == {"text": "r2", "scheduled_task_id": 2, "title": "task 2"}
    assert envelope.update_id == agent_task_update_id(2, agent_next_run)


async def test_scheduler_recurs_in_user_timezone_and_rolls_back_failed_send() -> None:
    pool = FakePool()
    pool.connection.tasks = [_task(1, "reminder", "30 23 * * 6")]

    def clock() -> datetime:
        return datetime(2026, 7, 10, 15, tzinfo=UTC)

    async def sender(_chat_id: int, _text: str) -> None:
        return None

    await SchedulerProcessor(pool, send_reply=sender, clock=clock).run_once()
    next_run = pool.connection.tasks[0]["next_run_at"]
    assert next_run > clock()
    assert next_run == datetime(2026, 7, 11, 20, 30, tzinfo=UTC)

    pool.connection.tasks = [_task(1, "reminder", None)]

    async def failing_sender(_chat_id: int, _text: str) -> None:
        raise RuntimeError("Telegram unavailable")

    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        await SchedulerProcessor(pool, send_reply=failing_sender).run_once()
    assert pool.connection.tasks[0]["status"] == "active"


async def test_scheduler_rolls_back_agent_task_when_enqueue_fails() -> None:
    pool = FakePool()
    pool.connection.tasks = [_task(1, "agent_task", "0 9 * * *")]
    before = pool.connection.tasks[0]["next_run_at"]

    async def sender(_chat_id: int, _text: str) -> None:
        return None

    async def failing_enqueue(_envelope: object) -> None:
        raise RuntimeError("redis unavailable")

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await SchedulerProcessor(
            pool, send_reply=sender, enqueue_background=failing_enqueue
        ).run_once()
    assert pool.connection.tasks[0]["next_run_at"] == before


def _task(task_id: int, kind: str, cron_expr: str | None) -> dict[str, Any]:
    return {
        "id": task_id,
        "kind": kind,
        "title": f"task {task_id}",
        "status": "active",
        "payload": f"r{task_id}",
        "cron_expr": cron_expr,
        "next_run_at": datetime.now(UTC) - timedelta(minutes=1),
        "tg_chat_id": 500,
        "tg_user_id": 100,
        "timezone": "Europe/Moscow",
    }


async def test_schedule_task_validates_cron_and_active_limit() -> None:
    pool = FakePool()
    broken = await _schedule_task(
        pool,
        _context(),
        ScheduleTaskArgs(prompt="prompt", cron="not cron", title="title"),
    )
    too_frequent = await _schedule_task(
        pool,
        _context(),
        ScheduleTaskArgs(prompt="prompt", cron="*/30 * * * *", title="title"),
    )
    created = await _schedule_task(
        pool,
        _context(),
        ScheduleTaskArgs(prompt="prompt", cron="0 9 * * *", title="title"),
    )

    assert broken.status == too_frequent.status == "error"
    assert broken.retryable is too_frequent.retryable is False
    assert created.status == "ok"
    assert pool.connection.tasks[0]["kind"] == "agent_task"
    assert pool.connection.tasks[0]["next_run_at"].utcoffset() == timedelta(0)

    pool.connection.tasks = [_task(index + 1, "agent_task", "0 9 * * *") for index in range(10)]
    limited = await _schedule_task(
        pool,
        _context(),
        ScheduleTaskArgs(prompt="prompt", cron="0 9 * * *", title="title"),
    )
    assert limited.status == "error"


async def test_scheduled_task_list_and_cancel_mapping() -> None:
    pool = FakePool()
    future = datetime.now(UTC) + timedelta(days=1)
    pool.connection.tasks = [
        {
            **_task(1, "agent_task", "0 9 * * *"),
            "payload": "x" * 250,
            "next_run_at": future,
        }
    ]

    listed = await _list_scheduled_tasks(pool, _context())
    cancelled = await _cancel_scheduled_task(pool, _context(), CancelScheduledTaskArgs(task_id=1))
    missing = await _cancel_scheduled_task(pool, _context(), CancelScheduledTaskArgs(task_id=1))

    assert listed.payload["tasks"] == [
        {
            "id": 1,
            "title": "task 1",
            "prompt": "x" * 200,
            "cron": "0 9 * * *",
            "next_run_at": future.astimezone(ZoneInfo("Europe/Moscow")).isoformat(),
        }
    ]
    assert cancelled.status == "ok"
    assert missing.status == "error"


def test_agent_task_update_id_is_deterministic() -> None:
    firing = datetime(2026, 7, 11, 9, tzinfo=UTC)
    assert agent_task_update_id(7, firing) == agent_task_update_id(7, firing)
    assert agent_task_update_id(7, firing) != agent_task_update_id(8, firing)
