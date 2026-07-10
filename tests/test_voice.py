"""Offline coverage for the voice envelope processor."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from core.stt import MockSTTProvider, STTResult, STTUnavailableError
from worker.voice import (
    DAILY_LIMIT_TEXT,
    EMPTY_TRANSCRIPT_TEXT,
    MAX_DAILY_VOICE_MINUTES,
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

    async def expire(self, name: str, seconds: int) -> bool:
        self.expiries[name] = seconds
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
