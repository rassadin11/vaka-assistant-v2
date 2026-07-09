"""A timezone-aware current-time tool used by the first agent loop."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from core.context import TaskContext
from core.llm import ToolDefinition

GET_CURRENT_TIME_DEFINITION = ToolDefinition(
    name="get_current_time",
    description="Get the current date and time in the user's timezone.",
    parameters={"type": "object", "properties": {}},
)

Clock = Callable[[], datetime]


async def get_current_time(context: TaskContext, *, clock: Clock | None = None) -> str:
    """Return the current time as ISO 8601 in the trusted user's timezone."""

    now = (clock if clock is not None else _utc_now)()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(ZoneInfo(context.timezone)).isoformat()


def _utc_now() -> datetime:
    return datetime.now(UTC)
