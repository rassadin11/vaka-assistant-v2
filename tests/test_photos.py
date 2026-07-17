"""Offline coverage for photo OCR envelope processing."""

# ruff: noqa: RUF001

from __future__ import annotations

from io import BytesIO
from uuid import UUID

import pytesseract
import pytest
from PIL import Image

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from worker.documents import MAX_MONTHLY_PAGES, MONTHLY_LIMIT_TEXT, _monthly_pages_key
from worker.photos import (
    EMPTY_PHOTO_TEXT,
    MAX_PHOTO_BYTES,
    PHOTO_OCR_UNAVAILABLE_TEXT,
    PHOTO_TOO_LARGE_TEXT,
    PhotoOcrProcessor,
)


class FakeRedis:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.expiries: dict[str, int] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def incrby(self, name: str, amount: int) -> int:
        value = int(self.values.get(name, "0")) + amount
        self.values[name] = str(value)
        return value

    async def expire(self, name: str, seconds: int) -> bool:
        self.expiries[name] = seconds
        return True


class RecordingInner:
    def __init__(self) -> None:
        self.envelopes: list[UpdateEnvelope] = []

    async def process(self, envelope: UpdateEnvelope, _context: TaskContext) -> str:
        self.envelopes.append(envelope)
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


def _envelope(**payload: object) -> UpdateEnvelope:
    return UpdateEnvelope(
        update_id=1,
        user_id=100,
        chat_id=500,
        kind="photo",
        payload={"tg_file_id": "photo-file", "size": 100, **payload},
        trace_id=_context().trace_id,
    )


def _image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="PNG")
    return output.getvalue()


async def test_ocr_rewrites_photo_envelope_and_passes_agent_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = RecordingInner()
    redis = FakeRedis()
    monkeypatch.setattr(
        "worker.photos.pytesseract.image_to_string", lambda _image, **_kwargs: "  milk  "
    )
    processor = PhotoOcrProcessor(
        redis,
        lambda _file_id, _maximum: _download_image(),
        inner,  # type: ignore[arg-type]
    )

    assert (
        await processor.process(_envelope(caption="чек из магазина"), _context()) == "agent reply"
    )

    assert inner.envelopes[0].kind == "text"
    assert inner.envelopes[0].payload == {
        "text": "Пользователь прислал фото.\nПодпись: чек из магазина\n"
        "Распознанный с фото текст:\nmilk"
    }
    assert redis.values[_monthly_pages_key(_context())] == "1"


async def test_empty_ocr_without_caption_does_not_call_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = RecordingInner()
    monkeypatch.setattr(
        "worker.photos.pytesseract.image_to_string", lambda _image, **_kwargs: " \t "
    )
    processor = PhotoOcrProcessor(
        FakeRedis(),
        lambda _file_id, _maximum: _download_image(),
        inner,  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(), _context()) == EMPTY_PHOTO_TEXT
    assert inner.envelopes == []


async def test_photo_without_file_size_is_still_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = RecordingInner()
    monkeypatch.setattr(
        "worker.photos.pytesseract.image_to_string", lambda _image, **_kwargs: "receipt"
    )
    processor = PhotoOcrProcessor(
        FakeRedis(),
        lambda _file_id, _maximum: _download_image(),
        inner,  # type: ignore[arg-type]
    )
    envelope = _envelope()
    envelope = envelope.model_copy(update={"payload": {"tg_file_id": "photo-file"}})

    assert await processor.process(envelope, _context()) == "agent reply"
    assert inner.envelopes[0].payload["text"].endswith("receipt")


async def test_oversized_photo_is_rejected_before_download() -> None:
    downloads = 0

    async def download(_file_id: str, _maximum: int) -> bytes:
        nonlocal downloads
        downloads += 1
        return _image_bytes()

    processor = PhotoOcrProcessor(FakeRedis(), download, RecordingInner())  # type: ignore[arg-type]

    assert (
        await processor.process(_envelope(size=MAX_PHOTO_BYTES + 1), _context())
        == PHOTO_TOO_LARGE_TEXT
    )
    assert downloads == 0


async def test_missing_tesseract_returns_unavailable_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_image: Image.Image, **_kwargs: object) -> str:
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr("worker.photos.pytesseract.image_to_string", unavailable)
    processor = PhotoOcrProcessor(
        FakeRedis(),
        lambda _file_id, _maximum: _download_image(),
        RecordingInner(),  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(), _context()) == PHOTO_OCR_UNAVAILABLE_TEXT


async def test_monthly_page_limit_rejects_without_agent_or_download() -> None:
    context = _context()
    redis = FakeRedis({_monthly_pages_key(context): str(MAX_MONTHLY_PAGES)})
    inner = RecordingInner()
    processor = PhotoOcrProcessor(
        redis,
        lambda _file_id, _maximum: _download_image(),
        inner,  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(), context) == MONTHLY_LIMIT_TEXT
    assert inner.envelopes == []


async def _download_image() -> bytes:
    return _image_bytes()
