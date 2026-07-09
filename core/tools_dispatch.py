"""Minimal tool dispatch boundary for the agent loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from core.context import TaskContext
from core.llm import LLMToolCall, ToolDefinition


class ToolDispatchError(RuntimeError):
    """Raised when a tool call cannot be dispatched."""


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

        try:
            handler = self._handlers[tool_call.name]
        except KeyError as exc:
            raise ToolDispatchError(f"Неизвестный инструмент: {tool_call.name}") from exc
        return await handler(context)
