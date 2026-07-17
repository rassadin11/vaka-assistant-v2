"""Shared domain operations for reminders and scheduled tasks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from core.calendar_view import CalendarTask

MAX_ACTIVE_REMINDERS = 25
TITLE_MAX_LENGTH = 80
ScheduledKind = Literal["reminder", "agent_task"]


class ReminderConnection(Protocol):
    """Subset of asyncpg used by the reminder domain service."""

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...

    async def execute(self, query: str, *args: Any) -> str: ...


class ReminderDomainError(Exception):
    """Base class for expected reminder domain failures."""


class ReminderTimeInPast(ReminderDomainError):
    """Raised when a requested reminder is not in the future."""


class ActiveReminderLimitReached(ReminderDomainError):
    """Raised when the user already has the maximum active reminders."""


class ScheduledTaskNotFound(ReminderDomainError):
    """Raised when a task is absent or hidden by RLS."""


class ScheduledTaskStateConflict(ReminderDomainError):
    """Raised when a visible task is no longer active."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"scheduled task is {status}")


async def create_reminder(
    connection: ReminderConnection,
    user_id: UUID,
    text: str,
    remind_at_utc: datetime,
    *,
    cron_expr: str | None = None,
    now: datetime | None = None,
) -> int:
    """Validate and insert one scheduled reminder inside the caller transaction."""

    current = now if now is not None else datetime.now(UTC)
    run_at = _as_utc(remind_at_utc)
    if run_at <= _as_utc(current):
        raise ReminderTimeInPast
    active_count = await connection.fetchval(
        """
        SELECT COUNT(*)
        FROM scheduled_tasks
        WHERE kind = 'reminder' AND status = 'active'
        """
    )
    if _integer(active_count or 0) >= MAX_ACTIVE_REMINDERS:
        raise ActiveReminderLimitReached
    reminder_id = await connection.fetchval(
        """
        INSERT INTO scheduled_tasks (
            user_id, kind, title, payload, cron_expr, next_run_at, status, created_at
        )
        VALUES ($1, 'reminder', $2, $3, $4, $5, 'active', now())
        RETURNING id
        """,
        user_id,
        text[:TITLE_MAX_LENGTH],
        text,
        cron_expr,
        run_at,
    )
    return _integer(reminder_id)


async def cancel_scheduled_task(
    connection: ReminderConnection,
    task_id: int,
    *,
    kind: ScheduledKind | None = None,
) -> ScheduledKind:
    """Cancel one visible active task and distinguish missing from state conflicts."""

    kind_filter = " AND kind = $2" if kind is not None else ""
    args: tuple[object, ...] = (task_id, kind) if kind is not None else (task_id,)
    row = await connection.fetchrow(
        f"""
        SELECT kind, status
        FROM scheduled_tasks
        WHERE id = $1{kind_filter}
        FOR UPDATE
        """,
        *args,
    )
    if row is None:
        raise ScheduledTaskNotFound
    row_status = str(row["status"])
    if row_status != "active":
        raise ScheduledTaskStateConflict(row_status)
    row_kind = str(row["kind"])
    if row_kind not in {"reminder", "agent_task"}:
        raise ScheduledTaskNotFound
    await connection.execute(
        "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = $1",
        task_id,
    )
    return row_kind  # type: ignore[return-value]


async def fetch_calendar_tasks(
    connection: ReminderConnection,
    range_start_utc: datetime,
    range_end_utc: datetime,
) -> list[CalendarTask]:
    """Load RLS-visible tasks required for calendar expansion."""

    rows = await connection.fetch(
        """
        SELECT id, kind, title, payload, cron_expr, next_run_at, status
        FROM scheduled_tasks
        WHERE status = 'active'
           OR (
                status = 'done'
                AND next_run_at >= $1
                AND next_run_at < $2
           )
        ORDER BY next_run_at, id
        """,
        _as_utc(range_start_utc),
        _as_utc(range_end_utc),
    )
    return [
        CalendarTask(
            id=int(row["id"]),
            kind=str(row["kind"]),
            title=str(row["title"] or ""),
            payload=str(row["payload"]),
            cron_expr=_optional_string(row["cron_expr"]),
            next_run_at=_datetime(row["next_run_at"]),
            status=str(row["status"]),
        )
        for row in rows
    ]


def cron_for_repeat(repeat: str, local_remind_at: datetime) -> str | None:
    """Build the exact cron expressions historically emitted by create_reminder."""

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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("domain datetime must be timezone-aware")
    return value.astimezone(UTC)


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("scheduled task next_run_at is not a datetime")
    return value


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("scheduled task cron_expr is not a string")


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("scheduled task id/count is not an integer")
    return value
