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
from core.limits import message_key
from core.llm import LLMMessage, LLMResponse, ToolDefinition
from core.llm_mock import MockLLMProvider, mock_text_response, mock_tool_call_response
from core.spend import spend_key
from core.tools_dispatch import StaticToolDispatcher
from worker.agent_processor import (
    LIMIT_APPROACH_BUDGET_TEXT,
    MESSAGE_LIMIT_TEXT,
    SOFT_REFUSE_TEXT,
    AgentProcessor,
)


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


def _agent_task_envelope(text: str = "scheduled request") -> UpdateEnvelope:
    return UpdateEnvelope.model_validate(
        {
            "update_id": 1_000_000_000_001,
            "user_id": 100,
            "chat_id": 500,
            "kind": "agent_task",
            "payload": {"text": text, "scheduled_task_id": 1, "title": "Morning brief"},
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


class _BudgetRedis:
    def __init__(self, spent: str) -> None:
        self.spent = spent
        self.notice_claims = 0
        self.message_increments = 0

    async def get(self, _name: str) -> str:
        return self.spent

    async def incrbyfloat(self, _name: str, _amount: float | Decimal) -> Decimal:
        return Decimal(0)

    async def expire(self, _name: str, _seconds: int) -> bool:
        return True

    async def incr(self, _name: str) -> int:
        self.message_increments += 1
        return self.message_increments

    async def set(
        self, _name: str, _value: str, *, ex: int | None = None, nx: bool = False
    ) -> bool:
        del ex, nx
        self.notice_claims += 1
        return self.notice_claims == 1


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


async def test_voice_transcript_persists_voice_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[MessageDraft]] = []

    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save(
        _pool: object, _user_id: UUID, drafts: list[MessageDraft], _trace: UUID
    ) -> list[UUID]:
        captured.append(drafts)
        return []

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    processor = _processor(MockLLMProvider.scripted([mock_text_response("answer")]))
    envelope = _envelope("voice transcript").model_copy(
        update={"payload": {"text": "voice transcript", "modality": "voice", "duration": 8}}
    )

    assert await processor.process(envelope, _context()) == "answer"
    assert captured[0][0].meta == {"modality": "voice", "duration": 8}


async def test_agent_task_uses_payload_text_background_usage_and_reply_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_queues: list[str] = []

    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save(*_args: object) -> list[UUID]:
        return []

    async def fake_save_usage(*args: object) -> None:
        saved_queues.append(cast(str, args[3]))

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", fake_save_usage)
    provider = MockLLMProvider.scripted([mock_text_response("scheduled answer")])
    processor = _processor(provider)

    assert (
        await processor.process(_agent_task_envelope("prepare the report"), _context())
        == "⏰ Morning brief:\nscheduled answer"
    )
    assert provider.calls[0].messages[-1] == LLMMessage(role="user", content="prepare the report")
    assert saved_queues == ["background"]


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


async def test_soft_refuse_does_not_call_the_llm() -> None:
    provider = MockLLMProvider.scripted([mock_text_response("must not be used")])
    redis = _BudgetRedis("22.5")
    processor = _processor(provider, queue_redis=redis)

    assert await processor.process(_envelope(), _context()) == SOFT_REFUSE_TEXT
    assert provider.calls == []
    assert redis.message_increments == 0


async def test_message_limit_boundary_refuses_without_incrementing() -> None:
    context = _context()
    key = message_key(context.user_id, context.timezone)

    class MessageRedis(_BudgetRedis):
        def __init__(self) -> None:
            super().__init__("0")
            self.values = {key: "100"}
            self.increments = 0

        async def get(self, name: str) -> str:
            return self.values.get(name, "0")

        async def incr(self, name: str) -> int:
            self.increments += 1
            self.values[name] = str(int(self.values.get(name, "0")) + 1)
            return int(self.values[name])

    redis = MessageRedis()
    provider = MockLLMProvider.scripted([mock_text_response("must not be used")])
    processor = _processor(provider, queue_redis=redis)

    assert await processor.process(_envelope(), context) == MESSAGE_LIMIT_TEXT
    assert provider.calls == []
    assert redis.values[key] == "100"
    assert redis.increments == 0


async def test_agent_task_is_not_subject_to_or_counted_by_message_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    key = message_key(context.user_id, context.timezone)

    class MessageRedis(_BudgetRedis):
        def __init__(self) -> None:
            super().__init__("0")
            self.values = {key: "100"}
            self.increments = 0

        async def get(self, name: str) -> str:
            return self.values.get(name, "0")

        async def incr(self, _name: str) -> int:
            self.increments += 1
            return self.increments

    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save(*_args: object) -> list[UUID]:
        return []

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    redis = MessageRedis()
    processor = _processor(
        MockLLMProvider.scripted([mock_text_response("scheduled answer")]), queue_redis=redis
    )

    assert (
        await processor.process(_agent_task_envelope(), context)
        == "⏰ Morning brief:\nscheduled answer"
    )
    assert redis.values[key] == "100"
    assert redis.increments == 0


async def test_message_counter_failure_does_not_block_agent_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRedis(_BudgetRedis):
        async def get(self, name: str) -> str:
            raise ConnectionError(name)

        async def incr(self, name: str) -> int:
            raise ConnectionError(name)

        async def incrbyfloat(self, name: str, amount: float | Decimal) -> Decimal:
            raise ConnectionError(f"{name}:{amount}")

    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save(*_args: object) -> list[UUID]:
        return []

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    provider = MockLLMProvider.scripted([mock_text_response("answer")])
    processor = _processor(provider, queue_redis=FailingRedis("0"))

    assert await processor.process(_envelope(), _context()) == "answer"
    assert len(provider.calls) == 1


async def test_background_budget_skip_notifies_only_once_per_local_day() -> None:
    provider = MockLLMProvider.scripted([mock_text_response("must not be used")])
    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    processor = AgentProcessor(
        provider,
        StaticToolDispatcher([], {}),
        AgentLoopConfig(),
        app_pool=object(),  # type: ignore[arg-type]
        send=send,
        queue_redis=_BudgetRedis("15"),
    )

    assert await processor.process(_agent_task_envelope(), _context()) is None
    assert await processor.process(_agent_task_envelope(), _context()) is None

    assert provider.calls == []
    assert sent == [
        (
            500,
            "⏰ Morning brief: фоновая задача пропущена — дневной лимит исчерпан, "
            "продолжится завтра",
        )
    ]


async def test_completed_text_task_notifies_for_independent_budget_and_message_axes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()

    class NoticeRedis:
        def __init__(self) -> None:
            self.values = {
                message_key(context.user_id, context.timezone): "79",
                spend_key(context.user_id, context.timezone): "12",
            }

        async def get(self, name: str) -> str | None:
            return self.values.get(name)

        async def incr(self, name: str) -> int:
            value = int(self.values.get(name, "0")) + 1
            self.values[name] = str(value)
            return value

        async def incrbyfloat(self, name: str, amount: float | Decimal) -> Decimal:
            value = Decimal(self.values.get(name, "0")) + Decimal(str(amount))
            self.values[name] = str(value)
            return value

        async def expire(self, _name: str, _seconds: int) -> bool:
            return True

        async def set(
            self, name: str, _value: str, *, ex: int | None = None, nx: bool = False
        ) -> bool:
            del ex
            if nx and name in self.values:
                return False
            self.values[name] = "1"
            return True

    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save(*_args: object) -> list[UUID]:
        return []

    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    processor = AgentProcessor(
        MockLLMProvider.scripted([mock_text_response("first"), mock_text_response("second")]),
        StaticToolDispatcher([], {}),
        AgentLoopConfig(),
        app_pool=object(),  # type: ignore[arg-type]
        send=send,
        queue_redis=NoticeRedis(),
    )

    assert await processor.process(_envelope(), context) == "first"
    await asyncio.sleep(0)
    assert await processor.process(_envelope(), context) == "second"
    await asyncio.sleep(0)

    assert sent == [
        (context.chat_id, LIMIT_APPROACH_BUDGET_TEXT),
        (context.chat_id, "⚠️ Использовано 80 из 100 сообщений на сегодня"),
    ]


async def test_budget_limit_approach_notifies_above_one_hundred_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(*_args: object) -> DialogHistory:
        return DialogHistory(summary=None, tail=[])

    async def fake_save(*_args: object) -> list[UUID]:
        return []

    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr("worker.agent_processor.load_dialog", fake_load)
    monkeypatch.setattr("worker.agent_processor.save_messages", fake_save)
    monkeypatch.setattr("worker.agent_processor.save_usage", _save_usage)
    processor = AgentProcessor(
        MockLLMProvider.scripted([mock_text_response("answer")]),
        StaticToolDispatcher([], {}),
        AgentLoopConfig(),
        app_pool=object(),  # type: ignore[arg-type]
        send=send,
        queue_redis=_BudgetRedis("16"),
    )

    assert await processor.process(_envelope(), _context()) == "answer"
    await asyncio.sleep(0)

    assert sent == [(_context().chat_id, LIMIT_APPROACH_BUDGET_TEXT)]
