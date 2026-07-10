"""Unit tests for context-aware dialogue processing."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from core.agent import AgentLoopConfig
from core.context import TaskContext
from core.context_manager import SummaryContext
from core.dialog_store import DialogHistory, MessageDraft, StoredMessage
from core.envelope import UpdateEnvelope
from core.llm import LLMMessage, LLMResponse, ToolDefinition
from core.llm_mock import MockLLMProvider, mock_text_response, mock_tool_call_response
from core.tools_dispatch import StaticToolDispatcher
from worker.agent_processor import AgentProcessor


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        update_id=1,
        timezone="Asia/Almaty",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


def _envelope(text: str = "new request") -> UpdateEnvelope:
    return UpdateEnvelope.model_validate(
        {
            "update_id": 1,
            "user_id": 100,
            "chat_id": 500,
            "kind": "text",
            "payload": {"text": text},
            "trace_id": str(_context().trace_id),
        }
    )


async def _send(_chat_id: int, _text: str) -> None:
    return None


class _RecordingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.logged = asyncio.Event()

    def exception(self, message: str) -> None:
        self.messages.append(message)
        self.logged.set()


def _processor(provider: MockLLMProvider, **kwargs: object) -> AgentProcessor:
    return AgentProcessor(
        provider,
        StaticToolDispatcher([], {}),
        AgentLoopConfig(),
        app_pool=object(),  # type: ignore[arg-type]
        send=_send,
        **kwargs,  # type: ignore[arg-type]
    )


async def _save_usage(*_args: object) -> None:
    return None


async def test_context_uses_loaded_summary_tail_and_trusted_dynamics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = DialogHistory(
        summary=SummaryContext("older summary", 2),
        tail=[
            StoredMessage(
                id=UUID("018f0000-0000-7000-8000-000000000010"),
                role="user",
                content="older tail",
                tool_calls=None,
                tool_call_id=None,
                tokens=2,
                meta={},
            )
        ],
    )
    captured: list[list[MessageDraft]] = []

    async def fake_load(*_args: object) -> DialogHistory:
        return history

    async def fake_save(
        _pool: object,
        _user_id: UUID,
        drafts: list[MessageDraft],
        _trace: UUID,
    ) -> list[UUID]:
        captured.append(drafts)
        return []

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    provider = MockLLMProvider.scripted([mock_text_response("answer")])
    processor = _processor(provider)

    assert await processor.process(_envelope(), _context()) == "answer"

    messages = provider.calls[0].messages
    system_content = messages[0].content or ""
    assert "older summary" in system_content
    assert "Asia/Almaty" in system_content
    assert messages[1].content == "older tail"
    assert messages[-1] == LLMMessage(role="user", content="new request")
    assert captured[0][0].content == "new request"


async def test_end_of_task_persists_user_tool_round_and_final_answer_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[list[MessageDraft]] = []

    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save(
        _pool: object,
        _user_id: UUID,
        drafts: list[MessageDraft],
        _trace: UUID,
    ) -> list[UUID]:
        saved.append(drafts)
        return []

    async def tool_handler(_context: TaskContext) -> str:
        return "tool result"

    provider = MockLLMProvider.scripted(
        [mock_tool_call_response("tool", "{}"), mock_text_response("final answer")]
    )
    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    processor = AgentProcessor(
        provider,
        StaticToolDispatcher(
            [ToolDefinition(name="tool", description="test tool", parameters={})],
            {"tool": tool_handler},
        ),
        AgentLoopConfig(),
        app_pool=object(),  # type: ignore[arg-type]
        send=_send,
    )

    assert await processor.process(_envelope(), _context()) == "final answer"

    assert len(saved) == 1
    drafts = saved[0]
    assert [draft.role for draft in drafts] == ["user", "assistant", "tool", "assistant"]
    assert drafts[0].content == "new request"
    assert drafts[1].tool_calls is not None
    assert drafts[2].content == "tool result"
    assert drafts[3].content == "final answer"
    assert drafts[3].meta == {"prompt_version": "v1", "stop_reason": "answer"}


async def test_trimmed_history_is_summarized_to_its_last_stored_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_id = UUID("018f0000-0000-7000-8000-000000000010")
    history = DialogHistory(
        summary=None,
        tail=[
            StoredMessage(
                id=old_id,
                role="user",
                content="old " * 4_000,
                tool_calls=None,
                tool_call_id=None,
                tokens=4_000,
                meta={},
            )
        ],
    )
    saved_summaries: list[tuple[UUID, str, UUID, int]] = []
    completed = asyncio.Event()

    async def fake_load(*_args: object) -> DialogHistory:
        return history

    async def fake_save_messages(*_args: object) -> list[UUID]:
        return []

    async def fake_save_summary(
        _pool: object, user_id: UUID, text: str, upto_message_id: UUID, tokens: int
    ) -> None:
        saved_summaries.append((user_id, text, upto_message_id, tokens))
        completed.set()

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save_messages)
    monkeypatch.setattr("worker.agent_processor.save_summary", fake_save_summary)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    processor = _processor(
        MockLLMProvider.scripted([mock_text_response("answer"), mock_text_response("summary")])
    )

    await processor.process(_envelope(), _context())
    await asyncio.wait_for(completed.wait(), timeout=1)

    assert saved_summaries[0][0] == _context().user_id
    assert saved_summaries[0][1] == "summary"
    assert saved_summaries[0][2] == old_id


async def test_summarization_failure_is_logged_without_affecting_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = DialogHistory(
        summary=None,
        tail=[
            StoredMessage(
                id=UUID("018f0000-0000-7000-8000-000000000010"),
                role="user",
                content="old " * 4_000,
                tool_calls=None,
                tool_call_id=None,
                tokens=4_000,
                meta={},
            )
        ],
    )

    async def fake_load(*_args: object) -> DialogHistory:
        return history

    async def fake_save_messages(*_args: object) -> list[UUID]:
        return []

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save_messages)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    logger = _RecordingLogger()
    processor = _processor(
        MockLLMProvider.scripted([mock_text_response("answer"), RuntimeError("unavailable")]),
        logger=logger,
    )

    assert await processor.process(_envelope(), _context()) == "answer"
    await asyncio.wait_for(logger.logged.wait(), timeout=1)

    assert logger.messages == ["dialogue summarization failed"]


async def test_history_within_budget_does_not_start_summarization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save(*_args: object) -> list[UUID]:
        return []

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    provider = MockLLMProvider.scripted([mock_text_response("answer")])
    processor = _processor(provider)

    await processor.process(_envelope(), _context())

    await asyncio.sleep(0)
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("responses", "config", "expected_stop_reason"),
    [
        ([mock_text_response("answer")], AgentLoopConfig(), "answer"),
        (
            [mock_tool_call_response("tool", "{}")],
            AgentLoopConfig(max_tool_calls=0),
            "tool_limit",
        ),
        (
            [mock_text_response("answer")],
            AgentLoopConfig(task_budget_rub=Decimal("1"), usd_rub_rate=Decimal("100")),
            "budget",
        ),
        (
            [
                mock_tool_call_response("unknown", "{}"),
                mock_tool_call_response("unknown", "{}"),
                mock_tool_call_response("unknown", "{}"),
            ],
            AgentLoopConfig(),
            "malformed",
        ),
    ],
)
async def test_processor_saves_usage_for_completed_stop_reasons(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[LLMResponse],
    config: AgentLoopConfig,
    expected_stop_reason: str,
) -> None:
    if expected_stop_reason == "budget":
        responses[0].usage.cost_usd = Decimal("0.02")
    saved: list[tuple[str, list[object]]] = []
    saved_messages: list[list[MessageDraft]] = []

    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save_messages(
        _pool: object,
        _user_id: UUID,
        drafts: list[MessageDraft],
        _trace_id: UUID,
    ) -> list[UUID]:
        saved_messages.append(drafts)
        return []

    async def fake_save_usage(
        _pool: object,
        _user_id: UUID,
        _trace_id: UUID,
        queue: str,
        records: Sequence[object],
    ) -> None:
        saved.append((queue, list(records)))

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save_messages)
    monkeypatch.setattr("worker.agent_processor.save_usage", fake_save_usage)
    processor = AgentProcessor(
        MockLLMProvider.scripted(responses),
        StaticToolDispatcher([], {}),
        config,
        app_pool=object(),  # type: ignore[arg-type]
        send=_send,
    )

    await processor.process(_envelope(), _context())

    assert saved[0][0] == "interactive"
    assert len(saved[0][1]) == len(responses)
    assert saved_messages[0][-1].meta == {
        "prompt_version": "v1",
        "stop_reason": expected_stop_reason,
    }


async def test_processor_saves_usage_when_agent_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FirstResponseThenHangs:
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

    saved: list[list[object]] = []
    saved_messages: list[list[MessageDraft]] = []

    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save_messages(
        _pool: object,
        _user_id: UUID,
        drafts: list[MessageDraft],
        _trace_id: UUID,
    ) -> list[UUID]:
        saved_messages.append(drafts)
        return []

    async def fake_save_usage(*args: object) -> None:
        saved.append(list(cast(Sequence[object], args[-1])))

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save_messages)
    monkeypatch.setattr("worker.agent_processor.save_usage", fake_save_usage)
    processor = AgentProcessor(
        FirstResponseThenHangs(),
        StaticToolDispatcher([], {}),
        AgentLoopConfig(task_timeout_seconds=0.01),
        app_pool=object(),  # type: ignore[arg-type]
        send=_send,
    )

    await processor.process(_envelope(), _context())

    assert len(saved[0]) == 1
    assert saved_messages[0][-1].meta == {"prompt_version": "v1", "stop_reason": "timeout"}
