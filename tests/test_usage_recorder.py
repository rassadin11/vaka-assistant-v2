"""Unit tests for per-request LLM usage recording."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

import pytest

from core.agent import AgentLoop, AgentLoopConfig
from core.context import TaskContext
from core.llm import LLMMessage, LLMResponse, ToolDefinition
from core.llm_mock import MockLLMProvider, mock_text_response, mock_tool_call_response
from core.tools_dispatch import StaticToolDispatcher
from core.usage_recorder import UsageRecordingProvider


async def test_recorder_adds_a_record_for_each_successful_generation() -> None:
    first = mock_text_response("first", model="first-model")
    first.usage.prompt_tokens = 12
    first.usage.completion_tokens = 3
    first.usage.cached_prompt_tokens = 4
    first.usage.cost_usd = None
    second = mock_text_response("second", model="second-model")
    second.usage.cost_usd = Decimal("0.00125")
    recorder = UsageRecordingProvider(MockLLMProvider.scripted([first, second]))

    await recorder.generate([LLMMessage(role="user", content="one")])
    await recorder.generate([LLMMessage(role="user", content="two")])

    assert recorder.records[0].model == "first-model"
    assert recorder.records[0].prompt_tokens == 12
    assert recorder.records[0].completion_tokens == 3
    assert recorder.records[0].cached_tokens == 4
    assert recorder.records[0].cost_usd == Decimal(0)
    assert recorder.records[1].cost_usd == Decimal("0.00125")


async def test_recorder_skips_failed_generation() -> None:
    recorder = UsageRecordingProvider(MockLLMProvider.scripted([RuntimeError("unavailable")]))

    with pytest.raises(RuntimeError, match="unavailable"):
        await recorder.generate([LLMMessage(role="user", content="hello")])

    assert recorder.records == []


async def test_timeout_retains_usage_recorded_before_a_hanging_call() -> None:
    class OneResponseThenHangs:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self,
            _messages: Sequence[LLMMessage],
            *,
            tools: Sequence[ToolDefinition] | None = None,
            temperature: float | None = None,
            max_tokens: int | None = None,
        ) -> LLMResponse:
            del tools, temperature, max_tokens
            self.calls += 1
            if self.calls == 1:
                return mock_tool_call_response("unknown", "{}")
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    recorder = UsageRecordingProvider(OneResponseThenHangs())
    loop = AgentLoop(
        recorder,
        StaticToolDispatcher([], {}),
        AgentLoopConfig(task_timeout_seconds=0.01),
    )
    context = TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        update_id=1,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )

    result = await loop.run([LLMMessage(role="user", content="hello")], context)

    assert result.stop_reason == "timeout"
    assert len(recorder.records) == 1
