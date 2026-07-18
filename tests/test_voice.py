"""Offline coverage for the voice envelope processor."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from core.limits import message_key
from core.spend import spend_key
from core.stt import MockSTTProvider, STTResult, STTUnavailableError
from worker.onboarding import HINT_VOICE_TEXT
from worker.voice import (
    DAILY_LIMIT_TEXT,
    EMPTY_TRANSCRIPT_TEXT,
    LIMIT_APPROACH_VOICE_TEXT,
    MAX_DAILY_VOICE_MINUTES,
    MESSAGE_LIMIT_TEXT,
    STT_UNAVAILABLE_TEXT,
    TOO_LONG_TEXT,
    VOICE_COUNTER_TTL_SECONDS,
    VOICE_UNAVAILABLE_TEXT,
    VoiceProcessor,
    _daily_minutes_key,
)


class FakeRedis:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.expiries: dict[str, int] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def incrby(self, name: str, amount: int) -> int:
        result = int(self.values.get(name, "0")) + amount
        self.values[name] = str(result)
        return result

    async def incrbyfloat(self, name: str, amount: float | Decimal) -> Decimal:
        result = Decimal(self.values.get(name, "0")) + Decimal(str(amount))
        self.values[name] = str(result)
        return result

    async def expire(self, name: str, seconds: int) -> bool:
        self.expiries[name] = seconds
        return True

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        del value, ex
        if nx and name in self.values:
            return False
        self.values[name] = "1"
        return True


class RecordingInner:
    def __init__(self) -> None:
        self.envelopes: list[UpdateEnvelope] = []
        self.contexts: list[TaskContext] = []

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str:
        self.envelopes.append(envelope)
        self.contexts.append(context)
        return "agent reply"


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        update_id=1,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


def _envelope(duration: float = 60) -> UpdateEnvelope:
    return UpdateEnvelope(
        update_id=1,
        user_id=100,
        chat_id=500,
        kind="voice",
        payload={"tg_file_id": "voice-file", "duration": duration, "size": 10},
        trace_id=_context().trace_id,
    )


async def test_long_voice_is_rejected_before_download_or_counter() -> None:
    download_calls = 0

    async def download(_file_id: str, _maximum: int) -> bytes:
        nonlocal download_calls
        download_calls += 1
        return b"voice"

    redis = FakeRedis()
    processor = VoiceProcessor(
        object(),
        redis,
        download,
        MockSTTProvider.scripted([]),
        RecordingInner(),  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(301), _context()) == TOO_LONG_TEXT
    assert download_calls == 0
    assert redis.values == {}


async def test_daily_limit_rejects_before_download() -> None:
    context = _context()
    key = _daily_minutes_key(context)
    redis = FakeRedis({key: str(MAX_DAILY_VOICE_MINUTES)})
    downloads = 0

    async def download(_file_id: str, _maximum: int) -> bytes:
        nonlocal downloads
        downloads += 1
        return b"voice"

    processor = VoiceProcessor(
        object(),
        redis,
        download,
        MockSTTProvider.scripted([]),
        RecordingInner(),  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(1), context) == DAILY_LIMIT_TEXT
    assert downloads == 0
    assert redis.values[key] == str(MAX_DAILY_VOICE_MINUTES)


async def test_success_rewrites_envelope_counts_utc_minutes_and_saves_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    redis = FakeRedis()
    inner = RecordingInner()
    saved: list[tuple[object, ...]] = []

    async def save_usage(*args: object) -> None:
        saved.append(args)

    monkeypatch.setattr("worker.voice.save_stt_usage", save_usage)
    provider = MockSTTProvider.scripted(
        [STTResult("  напомни купить хлеб  ", 61.2, Decimal("0.000061"))]
    )
    processor = VoiceProcessor(
        object(),
        redis,
        lambda _file_id, _maximum: _voice_bytes(),
        provider,
        inner,  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(61.2), context) == "agent reply"

    key = _daily_minutes_key(context)
    assert key.endswith(datetime.now(UTC).strftime("%Y%m%d"))
    assert redis.values[key] == "2"
    assert redis.expiries[key] == VOICE_COUNTER_TTL_SECONDS
    assert inner.contexts == [context]
    assert inner.envelopes[0].kind == "text"
    assert inner.envelopes[0].payload == {
        "text": "  напомни купить хлеб  ",
        "modality": "voice",
        "duration": 61.2,
    }
    assert saved == [(processor._app_pool, context.user_id, context.trace_id, Decimal("0.000061"))]


async def _voice_bytes() -> bytes:
    return b"voice"


async def test_empty_transcript_does_not_consume_minutes() -> None:
    redis = FakeRedis()
    processor = VoiceProcessor(
        object(),
        redis,
        lambda _file_id, _maximum: _voice_bytes(),
        MockSTTProvider.scripted([STTResult(" \t", 1, Decimal(0))]),
        RecordingInner(),
    )  # type: ignore[arg-type]

    assert await processor.process(_envelope(), _context()) == EMPTY_TRANSCRIPT_TEXT
    assert redis.values == {}


async def test_missing_provider_or_download_returns_polite_refusal() -> None:
    processor = VoiceProcessor(
        object(),
        FakeRedis(),
        None,
        None,
        RecordingInner(),  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(), _context()) == VOICE_UNAVAILABLE_TEXT


async def test_stt_failure_is_a_reply_not_a_processor_error() -> None:
    provider = MockSTTProvider.scripted([STTUnavailableError()])
    processor = VoiceProcessor(
        object(),
        FakeRedis(),
        lambda _file_id, _maximum: _voice_bytes(),
        provider,
        RecordingInner(),
    )  # type: ignore[arg-type]

    assert await processor.process(_envelope(), _context()) == STT_UNAVAILABLE_TEXT


async def test_soft_refuse_happens_before_download_or_stt() -> None:
    context = _context()
    redis = FakeRedis({spend_key(context.user_id, context.timezone): "22.5"})
    downloads = 0

    async def download(_file_id: str, _maximum: int) -> bytes:
        nonlocal downloads
        downloads += 1
        return b"voice"

    class NeverSTT:
        def __init__(self) -> None:
            self.calls = 0

        async def transcribe(self, _audio: bytes, _filename: str) -> STTResult:
            self.calls += 1
            raise AssertionError("STT must not be called")

    provider = NeverSTT()
    processor = VoiceProcessor(
        object(),
        redis,
        download,
        provider,
        RecordingInner(),  # type: ignore[arg-type]
    )

    assert "дневной лимит ассистента исчерпан" in (
        await processor.process(_envelope(), context) or ""
    )
    assert downloads == 0
    assert provider.calls == 0


async def test_message_limit_refuses_before_download_or_stt() -> None:
    context = _context()
    redis = FakeRedis({message_key(context.user_id, context.timezone): "100"})
    downloads = 0

    async def download(_file_id: str, _maximum: int) -> bytes:
        nonlocal downloads
        downloads += 1
        return b"voice"

    class NeverSTT:
        def __init__(self) -> None:
            self.calls = 0

        async def transcribe(self, _audio: bytes, _filename: str) -> STTResult:
            self.calls += 1
            raise AssertionError("STT must not be called")

    provider = NeverSTT()
    processor = VoiceProcessor(
        object(),
        redis,
        download,
        provider,
        RecordingInner(),  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(), context) == MESSAGE_LIMIT_TEXT
    assert downloads == 0
    assert provider.calls == 0


async def test_voice_limit_approach_notifies_once_after_reaching_eighty_percent() -> None:
    context = _context()
    redis = FakeRedis({_daily_minutes_key(context): "7"})
    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    processor = VoiceProcessor(
        object(),
        redis,
        lambda _file_id, _maximum: _voice_bytes(),
        MockSTTProvider.scripted(
            [STTResult("first", 1, Decimal(0)), STTResult("second", 1, Decimal(0))]
        ),
        RecordingInner(),  # type: ignore[arg-type]
        send=send,
    )

    assert await processor.process(_envelope(), context) == "agent reply"
    await asyncio.sleep(0)
    assert await processor.process(_envelope(), context) == "agent reply"
    await asyncio.sleep(0)

    assert sent == [
        (
            context.chat_id,
            LIMIT_APPROACH_VOICE_TEXT.format(used=8, limit=MAX_DAILY_VOICE_MINUTES),
        ),
        (context.chat_id, HINT_VOICE_TEXT),
    ]


async def test_voice_hint_redis_failure_is_fail_open() -> None:
    class FailingHintRedis(FakeRedis):
        async def set(
            self, name: str, value: str, *, ex: int | None = None, nx: bool = False
        ) -> bool:
            if name.startswith("onboarding:hint:"):
                raise ConnectionError("redis unavailable")
            return await super().set(name, value, ex=ex, nx=nx)

    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    processor = VoiceProcessor(
        object(),
        FailingHintRedis(),
        lambda _file_id, _maximum: _voice_bytes(),
        MockSTTProvider.scripted([STTResult("hello", 1, Decimal(0))]),
        RecordingInner(),  # type: ignore[arg-type]
        send=send,
    )

    assert await processor.process(_envelope(), _context()) == "agent reply"
    await asyncio.sleep(0)
    assert sent == []
