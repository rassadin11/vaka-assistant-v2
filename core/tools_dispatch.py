"""Minimal tool dispatch boundary for the agent loop."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from core.context import TaskContext
from core.llm import LLMToolCall, ToolDefinition


class ToolDispatchError(RuntimeError):
    """Raised when a tool call cannot be dispatched."""


class MalformedToolCallError(ToolDispatchError):
    """Raised when an LLM tool call has an invalid name or arguments."""


class ToolDispatcher(Protocol):
    """Expose tools to an LLM and execute them with trusted context."""

    def definitions(self) -> Sequence[ToolDefinition]:
        """Return the available definitions."""

    async def dispatch(self, tool_call: LLMToolCall, context: TaskContext) -> str:
        """Execute one tool call using worker-injected context."""


ToolHandler = Callable[[TaskContext], Awaitable[str]]


class StaticToolDispatcher:
    """Small name-to-handler dispatcher, to be replaced by the stage 4 registry."""

    def __init__(
        self,
        definitions: Sequence[ToolDefinition],
        handlers: Mapping[str, ToolHandler],
    ) -> None:
        self._definitions = tuple(definitions)
        self._handlers = dict(handlers)

    def definitions(self) -> Sequence[ToolDefinition]:
        """Return registered tool definitions."""

        return self._definitions

    async def dispatch(self, tool_call: LLMToolCall, context: TaskContext) -> str:
        """Dispatch by name; LLM arguments are deliberately not trusted context."""

        available_names = [definition.name for definition in self._definitions]
        if tool_call.name not in available_names:
            names = ", ".join(available_names)
            raise MalformedToolCallError(
                f"Неизвестный инструмент: {tool_call.name}. Доступные инструменты: {names}"
            )

        arguments_json = tool_call.arguments_json or "{}"
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise MalformedToolCallError(
                "Аргументы инструмента должны быть корректным JSON-объектом."
            ) from exc
        if not isinstance(arguments, dict):
            raise MalformedToolCallError(
                "Аргументы инструмента должны быть корректным JSON-объектом."
            )

        try:
            handler = self._handlers[tool_call.name]
        except KeyError as exc:
            raise ToolDispatchError(f"Инструмент недоступен: {tool_call.name}") from exc
        return await handler(context)
