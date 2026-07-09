"""Unit tests for the bounded LLM agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from core.agent import (
    BUDGET_TEXT,
    TIMEOUT_TEXT,
    TOOL_LIMIT_TEXT,
    AgentLoop,
    AgentLoopConfig,
)
from core.context import TaskContext
from core.llm import LLMMessage, LLMResponse, ToolDefinition
from core.llm_mock import MockLLMProvider, mock_text_response, mock_tool_call_response
from core.tools_dispatch import StaticToolDispatcher
from tools.clock import GET_CURRENT_TIME_DEFINITION, get_current_time


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        timezone="Asia/Almaty",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


def _dispatcher() -> StaticToolDispatcher:
    async def handler(context: TaskContext) -> str:
        return f"context-user={context.user_id}"

    return StaticToolDispatcher([GET_CURRENT_TIME_DEFINITION], {"get_current_time": handler})


async def test_text_answer_passes_through() -> None:
    loop = AgentLoop(MockLLMProvider.scripted([mock_text_response("Готово")]), _dispatcher())

    result = await loop.run([LLMMessage(role="user", content="привет")], _context())

    assert result.text == "Готово"
    assert result.stop_reason == "answer"
    assert result.llm_calls == 1
    assert result.tool_calls == 0


async def test_tool_round_trip_appends_trusted_result_to_next_llm_call() -> None:
    provider = MockLLMProvider.scripted(
        [mock_tool_call_response("get_current_time", "{}"), mock_text_response("Сейчас день")]
    )
    loop = AgentLoop(provider, _dispatcher())

    result = await loop.run([LLMMessage(role="user", content="который час")], _context())

    assert result.text == "Сейчас день"
    assert result.tool_calls == 1
    second_messages = provider.calls[1].messages
    assert second_messages[-1].role == "tool"
    assert second_messages[-1].content == f"context-user={_context().user_id}"
    assert second_messages[-1].tool_call_id == "mock-tool-call-1"


async def test_tool_limit_stops_before_dispatch() -> None:
    loop = AgentLoop(
        MockLLMProvider.scripted([mock_tool_call_response("get_current_time", "{}")]),
        _dispatcher(),
        AgentLoopConfig(max_tool_calls=0),
    )

    result = await loop.run([LLMMessage(role="user", content="который час")], _context())

    assert result.stop_reason == "tool_limit"
    assert result.text == TOOL_LIMIT_TEXT
    assert result.tool_calls == 0


async def test_budget_accumulates_costs_across_generations() -> None:
    first = mock_tool_call_response("get_current_time", "{}")
    first.usage.cost_usd = Decimal("0.02")
    second = mock_tool_call_response("get_current_time", "{}")
    second.usage.cost_usd = Decimal("0.02")
    loop = AgentLoop(
        MockLLMProvider.scripted([first, second]),
        _dispatcher(),
        AgentLoopConfig(task_budget_rub=Decimal("3"), usd_rub_rate=Decimal("100")),
    )

    result = await loop.run([LLMMessage(role="user", content="проверь")], _context())

    assert result.stop_reason == "budget"
    assert result.text == BUDGET_TEXT
    assert result.total_cost_usd == Decimal("0.04")
    assert result.llm_calls == 2


async def test_timeout_returns_fallback() -> None:
    class HangingProvider:
        async def generate(
            self,
            messages: Sequence[LLMMessage],
            *,
            tools: Sequence[ToolDefinition] | None = None,
            temperature: float | None = None,
            max_tokens: int | None = None,
        ) -> LLMResponse:
            del messages, tools, temperature, max_tokens
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    loop = AgentLoop(
        HangingProvider(),
        _dispatcher(),
        AgentLoopConfig(task_timeout_seconds=0.01),
    )

    result = await loop.run([LLMMessage(role="user", content="жду")], _context())

    assert result.stop_reason == "timeout"
    assert result.text == TIMEOUT_TEXT


async def test_progress_is_sent_once_for_a_long_tool_dispatch() -> None:
    progress: list[str] = []
    clock = [0.0]
    tool_started = asyncio.Event()
    allow_tool_to_finish = asyncio.Event()
    progress_sent = asyncio.Event()

    async def report(text: str) -> None:
        progress.append(text)
        progress_sent.set()

    async def sleep(seconds: float) -> None:
        clock[0] += seconds
        await tool_started.wait()

    async def slow_handler(context: TaskContext) -> str:
        del context
        tool_started.set()
        await allow_tool_to_finish.wait()
        return "time"

    provider = MockLLMProvider.scripted(
        [mock_tool_call_response("get_current_time", "{}"), mock_text_response("готово")]
    )
    loop = AgentLoop(
        provider,
        StaticToolDispatcher([GET_CURRENT_TIME_DEFINITION], {"get_current_time": slow_handler}),
        AgentLoopConfig(progress_after_seconds=0.001),
        clock=lambda: clock[0],
        sleep=sleep,
    )

    task = asyncio.create_task(
        loop.run(
            [LLMMessage(role="user", content="который час")],
            _context(),
            notify_progress=report,
        )
    )
    await progress_sent.wait()
    allow_tool_to_finish.set()
    result = await task

    assert result.text == "готово"
    assert progress == ["смотрю на часы…"]


async def test_clock_tool_uses_context_timezone() -> None:
    result = await get_current_time(
        _context(),
        clock=lambda: datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
    )

    assert result == "2026-07-10T17:00:00+05:00"
