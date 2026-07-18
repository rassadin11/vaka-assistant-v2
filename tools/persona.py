"""Assistant persona tools backed by the user-scoped database role."""

from __future__ import annotations

import json
import re
from typing import Literal, cast

import asyncpg
from pydantic import BaseModel, ConfigDict, field_validator

from core.context import TaskContext
from core.db import user_transaction
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

MAX_PERSONA_NAME_LENGTH = 30
MAX_PERSONA_STYLE_LENGTH = 200


class SetAssistantPersonaArgs(BaseModel):
    """Optional persona fields supplied by the model for a partial update."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    address: Literal["ty", "vy"] | None = None
    style: str | None = None

    @field_validator("address", mode="before")
    @classmethod
    def sanitize_address(cls, value: object) -> object:
        """Normalize surrounding whitespace before strict literal validation."""

        return _sanitize_text(value) if isinstance(value, str) else value


class ClearAssistantPersonaArgs(BaseModel):
    """Arguments for clearing the persona without model-controlled values."""

    model_config = ConfigDict(extra="ignore")


def register_persona_tools(registry: ToolRegistry, app_pool: asyncpg.Pool) -> None:
    """Register assistant persona mutations in the core tool profile."""

    async def set_assistant_persona(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _set_assistant_persona(
            app_pool,
            ctx,
            cast(SetAssistantPersonaArgs, args),
        )

    async def clear_assistant_persona(ctx: TaskContext, _args: BaseModel) -> ToolResult:
        return await _clear_assistant_persona(app_pool, ctx)

    registry.register(
        ToolSpec(
            name="set_assistant_persona",
            description=(
                "Set or partially update the assistant's user-configured name, address form, "
                "or communication style."
            ),
            args_schema=SetAssistantPersonaArgs,
            risk=RiskLevel.MUTATING_INTERNAL,
            handler=set_assistant_persona,
            daily_limit=20,
        )
    )
    registry.register(
        ToolSpec(
            name="clear_assistant_persona",
            description="Clear the assistant's user-configured persona and restore defaults.",
            args_schema=ClearAssistantPersonaArgs,
            risk=RiskLevel.MUTATING_INTERNAL,
            handler=clear_assistant_persona,
        )
    )


async def _set_assistant_persona(
    pool: asyncpg.Pool,
    context: TaskContext,
    args: SetAssistantPersonaArgs,
) -> ToolResult:
    updates: dict[str, str | None] = {}
    if "name" in args.model_fields_set:
        updates["name"] = _sanitize_text(args.name)
    if "address" in args.model_fields_set:
        updates["address"] = args.address
    if "style" in args.model_fields_set:
        updates["style"] = _sanitize_text(args.style)

    name = updates.get("name")
    if name is not None and len(name) > MAX_PERSONA_NAME_LENGTH:
        return ToolResult(
            status="error",
            error=f"Имя ассистента должно быть не длиннее {MAX_PERSONA_NAME_LENGTH} символов.",
            retryable=True,
        )
    style = updates.get("style")
    if style is not None and len(style) > MAX_PERSONA_STYLE_LENGTH:
        return ToolResult(
            status="error",
            error=f"Описание стиля должно быть не длиннее {MAX_PERSONA_STYLE_LENGTH} символов.",
            retryable=True,
        )

    async with user_transaction(pool, context.user_id) as connection:
        stored = _decode_profile(
            await connection.fetchval("SELECT assistant_profile::text FROM users")
        )
        for key, value in updates.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value
        await connection.execute(
            "UPDATE users SET assistant_profile = $1::jsonb, updated_at = now()",
            json.dumps(stored, ensure_ascii=False, separators=(",", ":")),
        )
    return ToolResult(status="ok", payload={"assistant_profile": stored})


async def _clear_assistant_persona(
    pool: asyncpg.Pool,
    context: TaskContext,
) -> ToolResult:
    async with user_transaction(pool, context.user_id) as connection:
        await connection.execute("UPDATE users SET assistant_profile = NULL, updated_at = now()")
    return ToolResult(status="ok", payload={"cleared": True})


def _sanitize_text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", value.strip())


def _decode_profile(value: object) -> dict[str, str]:
    if value is None:
        return {}
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        return {}
    return {
        key: item
        for key, item in decoded.items()
        if key in {"name", "address", "style"} and isinstance(item, str)
    }
