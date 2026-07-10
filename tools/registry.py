"""Stage-4 tool registrations kept separate from the registry infrastructure."""

from __future__ import annotations

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.embeddings import EmbeddingsProvider
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec
from tools.clock import get_current_time
from tools.finance import SendPhoto, register_finance_tools
from tools.memory import register_memory_tools
from tools.reminders import register_reminder_tools
from tools.web import CacheRedis, register_web_tools


class EmptyArgs(BaseModel):
    """Arguments for a tool which takes no model-controlled values."""

    model_config = ConfigDict(extra="ignore")


class EchoConfirmArgs(BaseModel):
    """Test-only external action arguments used by confirmation coverage."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(max_length=500)


async def _current_time(context: TaskContext, _args: BaseModel) -> ToolResult:
    return ToolResult(status="ok", payload={"time": await get_current_time(context)})


async def _echo_confirm(_context: TaskContext, args: BaseModel) -> ToolResult:
    """A test-only safe stand-in for an external action."""

    return ToolResult(status="ok", payload={"echo": args.model_dump()["text"]})


def register_builtin_tools(
    registry: ToolRegistry,
    app_pool: asyncpg.Pool | None = None,
    send_photo: SendPhoto | None = None,
    embeddings: EmbeddingsProvider | None = None,
    cache_redis: CacheRedis | None = None,
    searxng_url: str = "http://127.0.0.1:8091",
) -> None:
    """Register the intentionally small stage-4 baseline in stable order."""

    registry.register(
        ToolSpec(
            name="get_current_time",
            description="Get the current date and time in the user's timezone.",
            args_schema=EmptyArgs,
            risk=RiskLevel.READ_ONLY,
            handler=_current_time,
        )
    )
    if app_pool is not None:
        register_finance_tools(registry, app_pool, send_photo)
        register_reminder_tools(registry, app_pool)
        if embeddings is not None:
            register_memory_tools(registry, app_pool, embeddings)
    if cache_redis is None:
        raise ValueError("cache_redis is required to register built-in web tools")
    register_web_tools(registry, cache_redis, searxng_url)
    registry.register(
        ToolSpec(
            name="echo_confirm_test_only",
            description="Test-only external echo action used to verify confirmation delivery.",
            args_schema=EchoConfirmArgs,
            risk=RiskLevel.MUTATING_EXTERNAL,
            handler=_echo_confirm,
            daily_limit=20,
        )
    )
