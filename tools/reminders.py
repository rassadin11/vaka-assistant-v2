"""Reminder tools backed by the shared scheduled task table."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import BaseModel, ConfigDict

from core.context import TaskContext
from core.db import user_transaction
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

MAX_ACTIVE_REMINDERS = 25
TITLE_MAX_LENGTH = 80


class CreateReminderArgs(BaseModel):
    """Arguments for creating a one-off or recurring reminder."""

    model_config = ConfigDict(extra="ignore")

    text: str
    remind_at: str
    repeat: Literal["none", "daily", "weekly", "monthly"] = "none"


class EmptyArgs(BaseModel):
    """Arguments for a tool without model-controlled values."""

    model_config = ConfigDict(extra="ignore")


class CancelReminderArgs(BaseModel):
    """Arguments for cancelling one active reminder."""

    model_config = ConfigDict(extra="ignore")

    reminder_id: int


def register_reminder_tools(registry: ToolRegistry, app_pool: asyncpg.Pool) -> None:
    """Register reminder handlers with the worker-owned application pool."""

    async def create_reminder(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _create_reminder(app_pool, ctx, cast(CreateReminderArgs, args))

    async def list_reminders(ctx: TaskContext, _args: BaseModel) -> ToolResult:
        return await _list_reminders(app_pool, ctx)

    async def cancel_reminder(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _cancel_reminder(app_pool, ctx, cast(CancelReminderArgs, args))

    registry.register(
        ToolSpec(
            "create_reminder",
            "Create a reminder at a future date and time, optionally repeating.",
            CreateReminderArgs,
            RiskLevel.MUTATING_INTERNAL,
            create_reminder,
            daily_limit=30,
        )
    )
    registry.register(
        ToolSpec(
            "list_reminders",
            "List the user's active reminders ordered by their next run time.",
            EmptyArgs,
            RiskLevel.READ_ONLY,
            list_reminders,
        )
    )
    registry.register(
        ToolSpec(
            "cancel_reminder",
            "Cancel one active reminder by its id.",
            CancelReminderArgs,
            RiskLevel.MUTATING_INTERNAL,
            cancel_reminder,
        )
    )


async def _create_reminder(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: CreateReminderArgs,
) -> ToolResult:
    timezone = ZoneInfo(ctx.timezone)
    try:
        remind_at = _parse_remind_at(args.remind_at, timezone)
    except ValueError:
        return ToolResult(
            status="error",
            error="Время напоминания должно быть указано в формате ISO 8601.",
            retryable=True,
        )

    now = datetime.now(UTC)
    if remind_at <= now:
        local_now = now.astimezone(timezone).isoformat()
        return ToolResult(
            status="error",
            error=(
                f"Время напоминания должно быть в будущем. Сейчас по вашему времени: {local_now}."
            ),
            retryable=True,
        )

    cron_expr = _cron_for_repeat(args.repeat, remind_at.astimezone(timezone))
    async with user_transaction(pool, ctx.user_id) as connection:
        active_count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM scheduled_tasks
            WHERE kind = 'reminder' AND status = 'active'
            """
        )
        if int(active_count or 0) >= MAX_ACTIVE_REMINDERS:
            return ToolResult(
                status="error",
                error="Достигнут лимит: одновременно можно иметь не более 25 активных напоминаний.",
            )
        reminder_id = await connection.fetchval(
            """
            INSERT INTO scheduled_tasks (
                user_id, kind, title, payload, cron_expr, next_run_at, status, created_at
            )
            VALUES ($1, 'reminder', $2, $3, $4, $5, 'active', now())
            RETURNING id
            """,
            ctx.user_id,
            args.text[:TITLE_MAX_LENGTH],
            args.text,
            cron_expr,
            remind_at.astimezone(UTC),
        )
    return ToolResult(
        status="ok",
        payload={
            "id": int(reminder_id),
            "text": args.text,
            "next_run_at": remind_at.astimezone(timezone).isoformat(),
            "repeat": args.repeat,
        },
    )


async def _list_reminders(pool: asyncpg.Pool, ctx: TaskContext) -> ToolResult:
    timezone = ZoneInfo(ctx.timezone)
    async with user_transaction(pool, ctx.user_id) as connection:
        rows = await connection.fetch(
            """
            SELECT id, payload, cron_expr, next_run_at
            FROM scheduled_tasks
            WHERE kind = 'reminder' AND status = 'active'
            ORDER BY next_run_at, id
            LIMIT 25
            """
        )
    reminders = [
        {
            "id": int(row["id"]),
            "text": str(row["payload"]),
            "next_run_at": _as_datetime(row["next_run_at"]).astimezone(timezone).isoformat(),
            "repeat": _repeat_for_cron(_as_optional_str(row["cron_expr"])),
        }
        for row in rows
    ]
    return ToolResult(status="ok", payload={"reminders": reminders})


async def _cancel_reminder(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: CancelReminderArgs,
) -> ToolResult:
    async with user_transaction(pool, ctx.user_id) as connection:
        result = await connection.execute(
            """
            UPDATE scheduled_tasks
            SET status = 'cancelled'
            WHERE id = $1 AND kind = 'reminder' AND status = 'active'
            """,
            args.reminder_id,
        )
    if result == "UPDATE 0":
        return ToolResult(status="error", error="Активное напоминание не найдено.")
    return ToolResult(status="ok", payload={"id": args.reminder_id, "status": "cancelled"})


def _parse_remind_at(value: str, timezone: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed


def _cron_for_repeat(repeat: str, local_remind_at: datetime) -> str | None:
    minute = local_remind_at.minute
    hour = local_remind_at.hour
    if repeat == "none":
        return None
    if repeat == "daily":
        return f"{minute} {hour} * * *"
    if repeat == "weekly":
        return f"{minute} {hour} * * {(local_remind_at.weekday() + 1) % 7}"
    if repeat == "monthly":
        return f"{minute} {hour} {local_remind_at.day} * *"
    raise ValueError(f"unsupported reminder repeat: {repeat}")


def _repeat_for_cron(cron_expr: str | None) -> str:
    if cron_expr is None:
        return "none"
    fields = cron_expr.split()
    if len(fields) != 5:
        return "cron"
    minute, hour, day_of_month, month, day_of_week = fields
    if not (_in_range(minute, 0, 59) and _in_range(hour, 0, 23)):
        return "cron"
    if day_of_month == "*" and month == "*" and day_of_week == "*":
        return "daily"
    if day_of_month == "*" and month == "*" and _in_range(day_of_week, 0, 6):
        return "weekly"
    if _in_range(day_of_month, 1, 31) and month == "*" and day_of_week == "*":
        return "monthly"
    return "cron"


def _in_range(value: str, lower: int, upper: int) -> bool:
    return value.isdigit() and lower <= int(value) <= upper


def _as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("scheduled task next_run_at is not a datetime")
    return value


def _as_optional_str(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("scheduled task cron_expr is not a string")
