"""Provider-neutral agent loop for LLM tool use."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from core.context import TaskContext
from core.llm import LLMMessage, LLMProvider
from core.tools_dispatch import MalformedToolCallError, ToolDispatcher, ToolDispatchError

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
ProgressCallback = Callable[[str], Awaitable[None]]

TOOL_LIMIT_TEXT = "Задача оказалась слишком сложной — попробуйте разбить её на шаги."
TIMEOUT_TEXT = "Не успел закончить — попробуйте ещё раз или упростите запрос."
BUDGET_TEXT = (
    "Задача вышла слишком дорогой — остановил выполнение. Попробуйте сформулировать проще."
)
MALFORMED_TEXT = (
    "Не получилось корректно обратиться к инструментам — попробуйте переформулировать запрос."
)


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    """Limits for one agent task."""

    max_tool_calls: int = 10
    task_timeout_seconds: float = 120
    task_budget_rub: Decimal = Decimal("5")
    usd_rub_rate: Decimal = Decimal("100")
    progress_after_seconds: float = 15

    @classmethod
    def from_env(cls) -> AgentLoopConfig:
        """Load agent limits from environment variables."""

        return cls(
            max_tool_calls=int(os.getenv("AGENT_MAX_TOOL_CALLS", "10")),
            task_timeout_seconds=float(os.getenv("AGENT_TASK_TIMEOUT_SECONDS", "120")),
            task_budget_rub=Decimal(os.getenv("AGENT_TASK_BUDGET_RUB", "5")),
            usd_rub_rate=Decimal(os.getenv("USD_RUB_RATE", "100")),
        )


@dataclass(frozen=True, slots=True)
class AgentResult:
    """The final output and accounting for an agent task."""

    text: str
    stop_reason: Literal["answer", "tool_limit", "timeout", "budget", "malformed"]
    total_cost_usd: Decimal
    llm_calls: int
    tool_calls: int
    tool_names: tuple[str, ...] = ()


class AgentLoop:
    """Run LLM generation and sequential tool calls until a final answer."""

    def __init__(
        self,
        llm: LLMProvider,
        dispatcher: ToolDispatcher,
        config: AgentLoopConfig | None = None,
        *,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._llm = llm
        self._dispatcher = dispatcher
        self._config = config if config is not None else AgentLoopConfig.from_env()
        self._clock = clock if clock is not None else _monotonic
        self._sleep = sleep if sleep is not None else asyncio.sleep

    async def run(
        self,
        messages: list[LLMMessage],
        context: TaskContext,
        *,
        notify_progress: ProgressCallback | None = None,
    ) -> AgentResult:
        """Execute one bounded task, returning an answer or a safe fallback."""

        started_at = self._clock()
        active_tool_name: list[str | None] = [None]
        completed = asyncio.Event()
        progress_task: asyncio.Task[None] | None = None
        if notify_progress is not None:
            progress_task = asyncio.create_task(
                self._notify_progress_when_due(
                    started_at, active_tool_name, completed, notify_progress
                )
            )
        try:
            try:
                async with asyncio.timeout(self._config.task_timeout_seconds):
                    return await self._run_loop(messages, context, active_tool_name)
            except TimeoutError:
                return AgentResult(TIMEOUT_TEXT, "timeout", Decimal(0), 0, 0)
        finally:
            completed.set()
            if progress_task is not None:
                progress_task.cancel()
                with suppress(asyncio.CancelledError):
                    await progress_task

    async def _run_loop(
        self,
        messages: list[LLMMessage],
        context: TaskContext,
        active_tool_name: list[str | None],
    ) -> AgentResult:
        total_cost_usd = Decimal(0)
        llm_calls = 0
        tool_calls = 0
        invoked_tools: list[str] = []
        malformed_rounds = 0
        definitions = self._dispatcher.definitions()
        while True:
            response = await self._llm.generate(messages, tools=definitions)
            llm_calls += 1
            total_cost_usd += response.usage.cost_usd or Decimal(0)
            if total_cost_usd * self._config.usd_rub_rate > self._config.task_budget_rub:
                return AgentResult(BUDGET_TEXT, "budget", total_cost_usd, llm_calls, tool_calls)

            assistant = response.message
            tool_requests = assistant.tool_calls or []
            if not tool_requests:
                return AgentResult(
                    assistant.content or "",
                    "answer",
                    total_cost_usd,
                    llm_calls,
                    tool_calls,
                    tuple(invoked_tools),
                )
            if tool_calls + len(tool_requests) > self._config.max_tool_calls:
                return AgentResult(
                    TOOL_LIMIT_TEXT,
                    "tool_limit",
                    total_cost_usd,
                    llm_calls,
                    tool_calls,
                )

            messages.append(_with_sanitized_tool_calls(assistant))
            malformed_in_round = False
            for tool_call in tool_requests:
                active_tool_name[0] = tool_call.name
                invoked_tools.append(tool_call.name)
                try:
                    result_content = await self._dispatcher.dispatch(tool_call, context)
                except MalformedToolCallError as exc:
                    result_content = f"Ошибка инструмента: {exc}"
                    if not malformed_in_round:
                        malformed_rounds += 1
                        malformed_in_round = True
                except ToolDispatchError as exc:
                    result_content = f"Ошибка инструмента: {exc}"
                messages.append(
                    LLMMessage(role="tool", content=result_content, tool_call_id=tool_call.id)
                )
                tool_calls += 1
                active_tool_name[0] = None
                if malformed_rounds == 3:
                    return AgentResult(
                        MALFORMED_TEXT,
                        "malformed",
                        total_cost_usd,
                        llm_calls,
                        tool_calls,
                    )

    async def _notify_progress_when_due(
        self,
        started_at: float,
        active_tool_name: list[str | None],
        completed: asyncio.Event,
        notify_progress: ProgressCallback,
    ) -> None:
        await self._sleep(self._config.progress_after_seconds)
        if completed.is_set() or self._clock() - started_at < self._config.progress_after_seconds:
            return
        tool_name = active_tool_name[0]
        text = "смотрю на часы…" if tool_name == "get_current_time" else "работаю над задачей…"
        await notify_progress(text)


def _monotonic() -> float:
    return asyncio.get_running_loop().time()


def _with_sanitized_tool_calls(message: LLMMessage) -> LLMMessage:
    """Repair unparseable tool-call arguments before echoing the message back.

    Providers (confirmed on Parasail) validate every message in the request,
    including prior assistant turns, and reject the whole request when a
    historical tool call carries invalid JSON — which would make the malformed
    retry loop crash instead of self-correcting.
    """

    calls = message.tool_calls
    if not calls:
        return message
    sanitized = [
        call
        if _is_json_object(call.arguments_json)
        else call.model_copy(update={"arguments_json": "{}"})
        for call in calls
    ]
    if sanitized == calls:
        return message
    return message.model_copy(update={"tool_calls": sanitized})


def _is_json_object(arguments_json: str) -> bool:
    try:
        return isinstance(json.loads(arguments_json), dict)
    except json.JSONDecodeError:
        return False
