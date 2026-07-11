"""Validated tool registry, execution guardrails, and audit logging."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Literal, Protocol
from uuid import uuid4

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.context import TaskContext
from core.db import user_transaction
from core.llm import ToolDefinition
from core.metrics import active_metrics

LOGGER = logging.getLogger(__name__)
ToolStatus = Literal["ok", "error", "pending_confirmation"]
ButtonRows = list[list[tuple[str, str]]]
ConfirmationSender = Callable[[int, str, ButtonRows], Awaitable[None]]


class QueueRedis(Protocol):
    """Subset of the queue Redis client used for durable tool state."""

    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
        keepttl: bool = False,
    ) -> Awaitable[object]: ...

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...

    def incr(self, name: str) -> Awaitable[int]: ...

    def expire(self, name: str, time: int) -> Awaitable[bool]: ...

    def delete(self, *names: str) -> Awaitable[int]: ...


class RiskLevel(StrEnum):
    """Side-effect class which determines dispatch safeguards."""

    READ_ONLY = "read_only"
    MUTATING_INTERNAL = "mutating_internal"
    MUTATING_EXTERNAL = "mutating_external"


class ToolResult(BaseModel):
    """Compact structured result returned to the language model."""

    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    retryable: bool = False


ToolHandler = Callable[[TaskContext, BaseModel], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One LLM-facing tool and its trusted implementation."""

    name: str
    description: str
    args_schema: type[BaseModel]
    risk: RiskLevel
    handler: ToolHandler
    daily_limit: int | None = None

    def to_llm_definition(self) -> ToolDefinition:
        """Produce the deliberately small JSON Schema passed to the LLM."""

        schema = self.args_schema.model_json_schema()
        definitions = schema.pop("$defs", {})
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=_flatten_schema(schema, definitions),
        )


class ToolRegistry:
    """Own registered tools and safely dispatch their validated arguments."""

    def __init__(
        self,
        queue_redis: QueueRedis,
        app_pool: asyncpg.Pool,
        *,
        send_confirmation: ConfirmationSender | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._queue_redis = queue_redis
        self._app_pool = app_pool
        self._send_confirmation = send_confirmation
        self._logger = logger if logger is not None else LOGGER
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Register a uniquely named specification."""

        if spec.name in self._tools:
            raise ValueError(f"Tool is already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        """Return a specification by its stable LLM-visible name."""

        return self._tools.get(name)

    def get_for_context(
        self,
        ctx: TaskContext,
        connected: set[str] | None = None,
    ) -> list[ToolSpec]:
        """Return all tools in v1; profiles are introduced by later stages."""

        del ctx, connected
        return list(self._tools.values())

    async def dispatch(
        self,
        ctx: TaskContext,
        name: str,
        raw_args: Mapping[str, Any],
        call_ordinal: int,
    ) -> ToolResult:
        """Validate and execute one tool call, preserving an audit record."""

        started = monotonic()
        spec = self._tools.get(name)
        safe_args = _sanitize_args(raw_args)
        if spec is None:
            result = ToolResult(status="error", error="Неизвестный инструмент.")
            await self._log_attempt(ctx, name, safe_args, result, started)
            return result

        try:
            args = spec.args_schema.model_validate(safe_args)
        except ValidationError as exc:
            result = ToolResult(
                status="error",
                error=f"Некорректные аргументы инструмента: {_validation_message(exc)}",
                retryable=True,
            )
            await self._log_attempt(ctx, name, safe_args, result, started)
            return result

        try:
            result = await self._dispatch_validated(ctx, spec, args, call_ordinal)
        except Exception:
            self._logger.exception("tool handler failed", extra={"tool_name": name})
            result = ToolResult(
                status="error",
                error="Инструмент временно недоступен. Попробуйте позже.",
            )
        await self._log_attempt(ctx, name, safe_args, result, started)
        return result

    async def log_malformed(
        self,
        ctx: TaskContext,
        name: str,
        raw_args: Mapping[str, Any],
        error: str,
        started: float,
    ) -> None:
        """Audit malformed model calls without changing dispatcher error semantics."""

        await self._log_attempt(
            ctx,
            name,
            _sanitize_args(raw_args),
            ToolResult(status="error", error=error),
            started,
        )

    async def execute_outbox(
        self,
        ctx: TaskContext,
        name: str,
        raw_args: Mapping[str, Any],
    ) -> ToolResult:
        """Execute an already confirmed external action without requesting confirmation again."""

        started = monotonic()
        safe_args = _sanitize_args(raw_args)
        result = await self._execute_outbox(ctx, name, safe_args)
        await self._log_attempt(ctx, name, safe_args, result, started)
        return result

    async def _execute_outbox(
        self,
        ctx: TaskContext,
        name: str,
        safe_args: Mapping[str, Any],
    ) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(status="error", error="Инструмент больше недоступен.")
        try:
            args = spec.args_schema.model_validate(safe_args)
        except ValidationError:
            return ToolResult(status="error", error="Сохранённые аргументы действия некорректны.")
        try:
            return await spec.handler(ctx, args)
        except Exception:
            self._logger.exception("outbox tool handler failed", extra={"tool_name": name})
            return ToolResult(status="error", error="Не удалось выполнить подтверждённое действие.")

    async def _dispatch_validated(
        self,
        ctx: TaskContext,
        spec: ToolSpec,
        args: BaseModel,
        call_ordinal: int,
    ) -> ToolResult:
        idem_key: str | None = None
        if spec.risk is not RiskLevel.READ_ONLY:
            idem_key = f"idem:{ctx.update_id}:{call_ordinal}"
            claimed = await self._queue_redis.set(idem_key, "", ex=86_400, nx=True)
            if not bool(claimed):
                stored = await self._queue_redis.get(idem_key)
                if stored:
                    try:
                        return ToolResult.model_validate_json(_text(stored))
                    except ValueError:
                        self._logger.warning("invalid idempotency result key=%s", idem_key)
                return ToolResult(
                    status="error",
                    error="Возможно, действие уже было выполнено. Повторная отправка отключена.",
                )

        if spec.daily_limit is not None:
            count = await self._increment_daily_count(ctx, spec)
            if count > spec.daily_limit:
                result = ToolResult(
                    status="error",
                    error="Дневной лимит вызовов этого инструмента исчерпан. Попробуйте завтра.",
                )
                if idem_key is not None:
                    await self._queue_redis.set(
                        idem_key,
                        result.model_dump_json(),
                        xx=True,
                        keepttl=True,
                    )
                return result

        if spec.risk is RiskLevel.MUTATING_EXTERNAL:
            result = await self._create_pending_confirmation(ctx, spec, args)
        else:
            result = await spec.handler(ctx, args)

        if idem_key is not None:
            await self._queue_redis.set(
                idem_key,
                result.model_dump_json(),
                xx=True,
                keepttl=True,
            )
        return result

    async def _increment_daily_count(self, ctx: TaskContext, spec: ToolSpec) -> int:
        day = datetime.now(UTC).strftime("%Y%m%d")
        key = f"tool_cnt:{ctx.user_id}:{spec.name}:{day}"
        count = await self._queue_redis.incr(key)
        if count == 1:
            await self._queue_redis.expire(key, 172_800)
        return count

    async def _create_pending_confirmation(
        self,
        ctx: TaskContext,
        spec: ToolSpec,
        args: BaseModel,
    ) -> ToolResult:
        confirmation_id = str(uuid4())
        key = f"pending:{ctx.user_id}:{confirmation_id}"
        payload = {
            "tool_name": spec.name,
            "args": args.model_dump(mode="json"),
            "update_id": ctx.update_id,
            "chat_id": ctx.chat_id,
            "trace_id": str(ctx.trace_id),
        }
        await self._queue_redis.set(
            key,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ex=900,
        )
        if self._send_confirmation is None:
            await self._queue_redis.delete(key)
            return ToolResult(status="error", error="Не удалось запросить подтверждение действия.")
        try:
            await self._send_confirmation(
                ctx.chat_id,
                f"Подтвердите действие: {spec.name}.",
                [
                    [
                        ("Выполнить", f"confirm:{confirmation_id}"),
                        ("Отменить", f"cancel:{confirmation_id}"),
                    ]
                ],
            )
        except Exception:
            await self._queue_redis.delete(key)
            raise
        return ToolResult(
            status="pending_confirmation", payload={"confirmation_id": confirmation_id}
        )

    async def _log_attempt(
        self,
        ctx: TaskContext,
        name: str,
        args: Mapping[str, Any],
        result: ToolResult,
        started: float,
    ) -> None:
        """Best-effort RLS audit logging; logging must never alter tool output."""

        try:
            async with user_transaction(self._app_pool, ctx.user_id) as connection:
                await connection.execute(
                    """
                    INSERT INTO tool_calls_log (
                        user_id, trace_id, tool_name, args, result_status, error, latency_ms,
                        created_at
                    )
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, now())
                    """,
                    ctx.user_id,
                    ctx.trace_id,
                    name,
                    json.dumps(_truncate_strings(args), ensure_ascii=False, separators=(",", ":")),
                    result.status,
                    result.error,
                    int((monotonic() - started) * 1000),
                )
            active_metrics().tool_calls.labels(tool=name, status=result.status).inc()
        except Exception:
            self._logger.warning("tool call audit logging failed", exc_info=True)


def _sanitize_args(raw_args: Mapping[str, Any]) -> dict[str, Any]:
    """Remove model-provided identity before Pydantic receives the arguments."""

    return {key: value for key, value in raw_args.items() if key != "user_id"}


def _truncate_strings(value: Any) -> Any:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, Mapping):
        return {str(key): _truncate_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(item) for item in value]
    return value


def _validation_message(exc: ValidationError) -> str:
    error = exc.errors(include_url=False)[0]
    location = ".".join(str(item) for item in error["loc"])
    return f"поле {location}: {error['msg']}"


def _flatten_schema(value: Any, definitions: Mapping[str, Any]) -> Any:
    """Inline local references so providers receive no ``$defs`` section."""

    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            definition = definitions.get(name)
            if isinstance(definition, dict):
                merged = {
                    **definition,
                    **{key: item for key, item in value.items() if key != "$ref"},
                }
                return _flatten_schema(merged, definitions)
        return {
            key: _flatten_schema(item, definitions) for key, item in value.items() if key != "$defs"
        }
    if isinstance(value, list):
        return [_flatten_schema(item, definitions) for item in value]
    return value


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
