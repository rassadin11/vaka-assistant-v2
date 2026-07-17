"""Recurring agent-task scheduling tools backed by ``scheduled_tasks``."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from typing import cast
from zoneinfo import ZoneInfo

import asyncpg
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.db import user_transaction
from core.reminders_service import (
    ScheduledTaskNotFound,
    ScheduledTaskStateConflict,
    cancel_scheduled_task,
)
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

MAX_ACTIVE_SCHEDULED_TASKS = 10
PROMPT_MAX_LENGTH = 1_000
TITLE_MAX_LENGTH = 80
CRON_MAX_LENGTH = 100
PROMPT_LIST_MAX_LENGTH = 200


class ScheduleTaskArgs(BaseModel):
    """Arguments for creating a recurring agent task."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(min_length=1, max_length=PROMPT_MAX_LENGTH)
    cron: str = Field(min_length=1, max_length=CRON_MAX_LENGTH)
    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)


class EmptyArgs(BaseModel):
    """Arguments for a tool without model-controlled values."""

    model_config = ConfigDict(extra="ignore")


class CancelScheduledTaskArgs(BaseModel):
    """Arguments for cancelling one active scheduled agent task."""

    model_config = ConfigDict(extra="ignore")

    task_id: int


def register_scheduled_task_tools(registry: ToolRegistry, app_pool: asyncpg.Pool) -> None:
    """Register recurring agent-task handlers with the application pool."""

    async def schedule_task(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _schedule_task(app_pool, ctx, cast(ScheduleTaskArgs, args))

    async def list_scheduled_tasks(ctx: TaskContext, _args: BaseModel) -> ToolResult:
        return await _list_scheduled_tasks(app_pool, ctx)

    async def cancel_scheduled_task(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _cancel_scheduled_task(app_pool, ctx, cast(CancelScheduledTaskArgs, args))

    registry.register(
        ToolSpec(
            "schedule_task",
            "Schedule a recurring background agent task using a cron expression.",
            ScheduleTaskArgs,
            RiskLevel.MUTATING_INTERNAL,
            schedule_task,
            daily_limit=10,
        )
    )
    registry.register(
        ToolSpec(
            "list_scheduled_tasks",
            "List the user's active scheduled agent tasks ordered by next run time.",
            EmptyArgs,
            RiskLevel.READ_ONLY,
            list_scheduled_tasks,
        )
    )
    registry.register(
        ToolSpec(
            "cancel_scheduled_task",
            "Cancel one active scheduled agent task by its id.",
            CancelScheduledTaskArgs,
            RiskLevel.MUTATING_INTERNAL,
            cancel_scheduled_task,
        )
    )


async def _schedule_task(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: ScheduleTaskArgs,
) -> ToolResult:
    timezone = ZoneInfo(ctx.timezone)
    now = datetime.now(UTC).astimezone(timezone)
    try:
        first_run_at = _validate_cron(args.cron, now)
    except (TypeError, ValueError):
        return ToolResult(
            status="error",
            error="Некорректное cron-выражение.",
        )
    if first_run_at is None:
        return ToolResult(
            status="error",
            error="Расписание не может выполняться чаще одного раза в час.",
        )

    async with user_transaction(pool, ctx.user_id) as connection:
        active_count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM scheduled_tasks
            WHERE kind = 'agent_task' AND status = 'active'
            """
        )
        if int(active_count or 0) >= MAX_ACTIVE_SCHEDULED_TASKS:
            return ToolResult(
                status="error",
                error="Достигнут лимит: одновременно можно иметь не более 10 активных задач.",
            )
        task_id = await connection.fetchval(
            """
            INSERT INTO scheduled_tasks (
                user_id, kind, title, payload, cron_expr, next_run_at, status, created_at
            )
            VALUES ($1, 'agent_task', $2, $3, $4, $5, 'active', now())
            RETURNING id
            """,
            ctx.user_id,
            args.title,
            args.prompt,
            args.cron,
            first_run_at.astimezone(UTC),
        )
    return ToolResult(
        status="ok",
        payload={
            "id": int(task_id),
            "title": args.title,
            "prompt": args.prompt,
            "cron": args.cron,
            "next_run_at": first_run_at.isoformat(),
        },
    )


async def _list_scheduled_tasks(pool: asyncpg.Pool, ctx: TaskContext) -> ToolResult:
    timezone = ZoneInfo(ctx.timezone)
    async with user_transaction(pool, ctx.user_id) as connection:
        rows = await connection.fetch(
            """
            SELECT id, title, payload, cron_expr, next_run_at
            FROM scheduled_tasks
            WHERE kind = 'agent_task' AND status = 'active'
            ORDER BY next_run_at, id
            LIMIT 10
            """
        )
    tasks = [
        {
            "id": int(row["id"]),
            "title": str(row["title"]),
            "prompt": str(row["payload"])[:PROMPT_LIST_MAX_LENGTH],
            "cron": str(row["cron_expr"]),
            "next_run_at": _as_datetime(row["next_run_at"]).astimezone(timezone).isoformat(),
        }
        for row in rows
    ]
    return ToolResult(status="ok", payload={"tasks": tasks})


async def _cancel_scheduled_task(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: CancelScheduledTaskArgs,
) -> ToolResult:
    async with user_transaction(pool, ctx.user_id) as connection:
        try:
            await cancel_scheduled_task(connection, args.task_id, kind="agent_task")
        except (ScheduledTaskNotFound, ScheduledTaskStateConflict):
            return ToolResult(status="error", error="Активная задача не найдена.")
    return ToolResult(status="ok", payload={"id": args.task_id, "status": "cancelled"})


def _validate_cron(cron_expr: str, now: datetime) -> datetime | None:
    """Return the first run or ``None`` when consecutive runs are under one hour apart."""

    iterator = croniter(cron_expr, now)
    firings = [iterator.get_next(datetime) for _ in range(5)]
    if any(
        (later.astimezone(UTC) - earlier.astimezone(UTC)).total_seconds() < 3_600
        for earlier, later in pairwise(firings)
    ):
        return None
    return firings[0]


def _as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("scheduled task next_run_at is not a datetime")
    return value
