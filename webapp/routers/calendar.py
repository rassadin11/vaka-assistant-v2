"""Calendar and scheduled-task endpoints for the Telegram Mini App."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, FastAPI, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field

from core.calendar_view import CalendarOccurrence, expand_occurrences
from core.rate_limit import RateLimitRedis
from core.reminders_service import (
    ActiveReminderLimitReached,
    ReminderConnection,
    ReminderTimeInPast,
    ScheduledTaskNotFound,
    ScheduledTaskStateConflict,
    cancel_scheduled_task,
    create_reminder,
    fetch_calendar_tasks,
)
from webapp.dependencies import active_request_user, bearer_subject, require_webapp_rate_limit
from webapp.errors import WebAppError
from webapp.metrics import WebAppMetrics

PoolGetter = Callable[[], asyncpg.Pool]
CacheGetter = Callable[[], RateLimitRedis]
Clock = Callable[[], datetime]
LOGGER = logging.getLogger(__name__)
MAX_CALENDAR_DAYS = 62


class CalendarOccurrenceResponse(BaseModel):
    """One occurrence grouped under its local date."""

    id: int
    kind: Literal["reminder", "agent_task"]
    text: str
    time_local: str
    occurs_at: str
    recurring: bool
    repeat_human: str | None
    status: Literal["active", "done"]
    truncated: bool = False


class CalendarResponse(BaseModel):
    """Calendar occurrences keyed by ISO local date."""

    days: dict[str, list[CalendarOccurrenceResponse]]


class CreateReminderRequest(BaseModel):
    """Transport-only constraints for a Mini App one-off reminder."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    remind_at_local: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


class CreatedReminderResponse(BaseModel):
    """Created one-off reminder identity and local firing time."""

    id: int
    text: str
    next_run_at: str
    status: Literal["active"] = "active"


class CancelledScheduledResponse(BaseModel):
    """Successful scheduled-task cancellation."""

    id: int
    status: Literal["cancelled"] = "cancelled"


def install_calendar_routes(
    app: FastAPI,
    *,
    pool: PoolGetter,
    cache: CacheGetter,
    session_secret: str,
    metrics: WebAppMetrics,
    clock: Clock,
) -> None:
    """Attach calendar routes with injectable runtime dependencies."""

    router = APIRouter(prefix="/app/api")

    @router.get("/calendar", response_model=CalendarResponse)
    async def calendar(
        from_date: Annotated[date, Query(alias="from")],
        to_date: Annotated[date, Query(alias="to")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> CalendarResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        day_count = (to_date - from_date).days + 1
        if day_count < 1 or day_count > MAX_CALENDAR_DAYS:
            raise WebAppError(
                400,
                "invalid_calendar_range",
                "Диапазон календаря должен быть от 1 до 62 дней.",
            )
        async with active_request_user(pool(), user_id) as user:
            timezone = ZoneInfo(user.timezone)
            range_start = datetime.combine(from_date, time.min, tzinfo=timezone)
            range_end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone)
            tasks = await fetch_calendar_tasks(
                cast(ReminderConnection, user.connection),
                range_start.astimezone(UTC),
                range_end.astimezone(UTC),
            )
            occurrences = expand_occurrences(tasks, from_date, to_date, timezone)
        days: defaultdict[str, list[CalendarOccurrenceResponse]] = defaultdict(list)
        for occurrence in occurrences:
            days[occurrence.local_date].append(_occurrence_response(occurrence))
        return CalendarResponse(days=dict(days))

    @router.post(
        "/reminders",
        response_model=CreatedReminderResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_one_off_reminder(
        payload: CreateReminderRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CreatedReminderResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        local_naive = datetime.strptime(payload.remind_at_local, "%Y-%m-%dT%H:%M")
        async with active_request_user(pool(), user_id) as user:
            timezone = ZoneInfo(user.timezone)
            local_run_at = local_naive.replace(tzinfo=timezone)
            try:
                reminder_id = await create_reminder(
                    cast(ReminderConnection, user.connection),
                    user_id,
                    payload.text,
                    local_run_at.astimezone(UTC),
                    now=clock(),
                )
            except ReminderTimeInPast as exc:
                raise WebAppError(
                    422,
                    "reminder_time_in_past",
                    "Это время уже прошло",
                ) from exc
            except ActiveReminderLimitReached as exc:
                raise WebAppError(
                    422,
                    "active_reminder_limit",
                    "Достигнут лимит: одновременно можно иметь не более 25 активных напоминаний.",
                ) from exc
        metrics.reminders_created.inc()
        LOGGER.info("Mini App reminder created", extra={"scheduled_kind": "reminder"})
        return CreatedReminderResponse(
            id=reminder_id,
            text=payload.text,
            next_run_at=local_run_at.isoformat(),
        )

    @router.delete("/scheduled/{task_id}", response_model=CancelledScheduledResponse)
    async def cancel_task(
        task_id: int,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CancelledScheduledResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        async with active_request_user(pool(), user_id) as user:
            try:
                task_kind = await cancel_scheduled_task(
                    cast(ReminderConnection, user.connection), task_id
                )
            except ScheduledTaskNotFound as exc:
                raise WebAppError(404, "scheduled_not_found", "Задача не найдена.") from exc
            except ScheduledTaskStateConflict as exc:
                raise WebAppError(
                    409,
                    "scheduled_state_conflict",
                    "Эту задачу уже нельзя отменить.",
                ) from exc
        metrics.reminders_cancelled.inc()
        LOGGER.info("Mini App scheduled task cancelled", extra={"scheduled_kind": task_kind})
        return CancelledScheduledResponse(id=task_id)

    app.include_router(router)


def _occurrence_response(occurrence: CalendarOccurrence) -> CalendarOccurrenceResponse:
    return CalendarOccurrenceResponse(
        id=occurrence.id,
        kind=cast(Literal["reminder", "agent_task"], occurrence.kind),
        text=occurrence.text,
        time_local=occurrence.time_local,
        occurs_at=occurrence.occurs_at,
        recurring=occurrence.recurring,
        repeat_human=occurrence.repeat_human,
        status=cast(Literal["active", "done"], occurrence.status),
        truncated=occurrence.truncated,
    )
