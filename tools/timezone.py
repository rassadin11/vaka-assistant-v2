"""User timezone tool backed by the user-scoped database role."""

from __future__ import annotations

from typing import cast

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.db import user_transaction
from core.timezones import local_time_fields, resolve_timezone
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

MAX_CITY_LENGTH = 64
UNKNOWN_CITY_ERROR = "Не узнал город. Назовите крупный город рядом — например, Самара."  # noqa: RUF001

RECURRING_REMINDERS_QUERY = """
SELECT COUNT(*)
FROM scheduled_tasks
WHERE kind = 'reminder' AND status = 'active' AND cron_expr IS NOT NULL
"""


class SetTimezoneArgs(BaseModel):
    """A city name, or an IANA identifier, supplied by the model."""

    model_config = ConfigDict(extra="ignore")

    city: str = Field(max_length=MAX_CITY_LENGTH)


def register_timezone_tools(registry: ToolRegistry, app_pool: asyncpg.Pool) -> None:
    """Register the user timezone mutation in the core tool profile."""

    async def set_timezone(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _set_timezone(app_pool, ctx, cast(SetTimezoneArgs, args))

    registry.register(
        ToolSpec(
            name="set_timezone",
            description=(
                "Change the user's timezone when they move or report that the assistant's "
                "time is wrong. Pass the city in the user's own words."
            ),
            args_schema=SetTimezoneArgs,
            risk=RiskLevel.MUTATING_INTERNAL,
            handler=set_timezone,
            daily_limit=3,
        )
    )


async def _set_timezone(
    pool: asyncpg.Pool,
    context: TaskContext,
    args: SetTimezoneArgs,
) -> ToolResult:
    timezone = resolve_timezone(args.city)
    if timezone is None:
        return ToolResult(status="error", error=UNKNOWN_CITY_ERROR, retryable=True)

    unchanged = timezone == context.timezone
    async with user_transaction(pool, context.user_id) as connection:
        if not unchanged:
            await connection.execute(
                "UPDATE users SET timezone = $1, updated_at = now()",
                timezone,
            )
        recurring = await connection.fetchval(RECURRING_REMINDERS_QUERY)

    payload: dict[str, object] = {
        "timezone": timezone,
        "recurring_reminders": int(recurring or 0),
        **local_time_fields(timezone),
    }
    if unchanged:
        payload["unchanged"] = True
    return ToolResult(status="ok", payload=payload)
