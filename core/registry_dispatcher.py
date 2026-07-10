"""Agent-loop adapter for the validated stage-4 tool registry."""

from __future__ import annotations

import json
from collections.abc import Sequence
from time import monotonic

from core.context import TaskContext
from core.llm import LLMToolCall, ToolDefinition
from core.tools import ToolRegistry
from core.tools_dispatch import MalformedToolCallError


class RegistryToolDispatcher:
    """Keep task-local ordinals and validation retries around a shared registry."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._call_ordinal = 0
        self._validation_retries: dict[str, int] = {}

    def definitions(self) -> Sequence[ToolDefinition]:
        """Return current v1 definitions in registration order."""

        return [spec.to_llm_definition() for spec in self._registry.get_for_context(_placeholder())]

    async def dispatch(self, tool_call: LLMToolCall, context: TaskContext) -> str:
        """Convert a model call to a compact serialized ``ToolResult``."""

        started = monotonic()
        if self._registry.get(tool_call.name) is None:
            names = ", ".join(spec.name for spec in self._registry.get_for_context(context))
            error = f"Неизвестный инструмент: {tool_call.name}. Доступные инструменты: {names}"
            await self._registry.log_malformed(context, tool_call.name, {}, error, started)
            raise MalformedToolCallError(error)
        try:
            parsed = json.loads(tool_call.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            error = "Аргументы инструмента должны быть корректным JSON-объектом."
            await self._registry.log_malformed(
                context, tool_call.name, {"raw": tool_call.arguments_json}, error, started
            )
            raise MalformedToolCallError(error) from exc
        if not isinstance(parsed, dict):
            error = "Аргументы инструмента должны быть корректным JSON-объектом."
            await self._registry.log_malformed(
                context, tool_call.name, {"raw": tool_call.arguments_json}, error, started
            )
            raise MalformedToolCallError(error)

        self._call_ordinal += 1
        result = await self._registry.dispatch(context, tool_call.name, parsed, self._call_ordinal)
        if result.retryable:
            retries = self._validation_retries.get(tool_call.name, 0) + 1
            self._validation_retries[tool_call.name] = retries
            if retries > 2:
                result = result.model_copy(update={"retryable": False})
        return result.model_dump_json()


def _placeholder() -> TaskContext:
    """Definitions are context-independent in registry v1."""

    from uuid import UUID

    return TaskContext(
        user_id=UUID(int=0),
        tg_user_id=0,
        chat_id=0,
        update_id=0,
        timezone="UTC",
        plan="trial",
        trace_id=UUID(int=0),
    )
