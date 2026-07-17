"""Unit tests for Telegram sender pacing and retry behavior."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile

from core.telegram_sender import MAX_TELEGRAM_DOWNLOAD_BYTES, SendRateLimiter, TelegramSender


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object | None]] = []
        self.message_kwargs: list[dict[str, Any]] = []
        self.actions: list[tuple[int, str]] = []
        self.callback_answers: list[str] = []
        self.message_outcomes: list[object] = []
        self.photos: list[tuple[int, BufferedInputFile, str | None]] = []
        self.photo_outcomes: list[object] = []
        self.downloaded_paths: list[str] = []
        self.download_data = b"file"

    async def send_message(
        self,
        chat_id: int,
        text: str,
        **kwargs: Any,
    ) -> object:
        self.messages.append((chat_id, text, kwargs.get("reply_markup")))
        self.message_kwargs.append(kwargs)
        if self.message_outcomes:
            outcome = self.message_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return object()

    async def send_chat_action(self, chat_id: int, action: str) -> object:
        self.actions.append((chat_id, action))
        return object()

    async def send_photo(
        self,
        chat_id: int,
        photo: BufferedInputFile,
        caption: str | None = None,
    ) -> object:
        self.photos.append((chat_id, photo, caption))
        if self.photo_outcomes:
            outcome = self.photo_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return object()

    async def answer_callback_query(self, callback_query_id: str) -> object:
        self.callback_answers.append(callback_query_id)
        return object()

    async def get_file(self, file_id: str) -> object:
        return SimpleNamespace(file_path=f"files/{file_id}")

    async def download_file(
        self, file_path: str, destination: BytesIO, *, timeout: int = 30
    ) -> object:
        del timeout
        self.downloaded_paths.append(file_path)
        destination.write(self.download_data)
        return destination


async def test_rate_limiter_paces_same_chat_messages() -> None:
    clock = FakeClock()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)

    await limiter.wait(chat_id=1)
    await limiter.wait(chat_id=1)

    assert clock.sleeps == [1.0]


async def test_rate_limiter_paces_global_messages() -> None:
    clock = FakeClock()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)

    await limiter.wait(chat_id=1)
    await limiter.wait(chat_id=2)

    assert clock.sleeps == [pytest.approx(1 / 30)]


async def test_sender_retries_telegram_429_retry_after() -> None:
    clock = FakeClock()
    bot = FakeBot()
    bot.message_outcomes = [
        TelegramRetryAfter(method=cast(Any, None), message="retry", retry_after=2),
        object(),
    ]
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep)

    await sender.send_message(42, "hello")

    assert [text for _, text, _markup in bot.messages] == ["hello", "hello"]
    assert [kwargs["parse_mode"] for kwargs in bot.message_kwargs] == ["HTML", "HTML"]
    assert clock.sleeps == [2.0]


async def test_sender_sends_png_photo_with_same_429_retry_policy() -> None:
    clock = FakeClock()
    bot = FakeBot()
    bot.photo_outcomes = [
        TelegramRetryAfter(method=cast(Any, None), message="retry", retry_after=2),
        object(),
    ]
    sender = TelegramSender(
        bot, limiter=SendRateLimiter(clock=clock, sleep=clock.sleep), sleep=clock.sleep
    )

    await sender.send_photo(42, b"png", "chart")

    assert [(chat_id, photo.data, caption) for chat_id, photo, caption in bot.photos] == [
        (42, b"png", "chart"),
        (42, b"png", "chart"),
    ]
    assert clock.sleeps == [2.0]


async def test_sender_splits_long_messages() -> None:
    clock = FakeClock()
    bot = FakeBot()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep)
    text = ("a" * 4096) + "\n" + ("b" * 10)

    await sender.send_message(42, text)

    assert bot.messages == [(42, "a" * 4096, None), (42, "b" * 10, None)]
    assert [kwargs["parse_mode"] for kwargs in bot.message_kwargs] == ["HTML", "HTML"]
    assert clock.sleeps == [1.0]


async def test_sender_applies_reply_markup_to_last_chunk_only() -> None:
    clock = FakeClock()
    bot = FakeBot()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep)
    markup = object()
    text = ("a" * 4096) + "\n" + ("b" * 10)

    await sender.send_message(42, text, reply_markup=markup)

    assert bot.messages == [(42, "a" * 4096, None), (42, "b" * 10, markup)]
    assert [kwargs["parse_mode"] for kwargs in bot.message_kwargs] == ["HTML", "HTML"]


async def test_sender_falls_back_to_plain_text_after_html_parse_error() -> None:
    bot = FakeBot()
    bot.message_outcomes = [
        TelegramBadRequest(method=cast(Any, None), message="can't parse entities"),
        object(),
    ]
    sender = TelegramSender(bot)

    await sender.send_message(42, "**bold**")

    assert bot.messages == [(42, "<b>bold</b>", None), (42, "**bold**", None)]
    assert bot.message_kwargs[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in bot.message_kwargs[1]


async def test_admin_notifications_remain_plain_text() -> None:
    bot = FakeBot()
    sender = TelegramSender(bot, admin_chat_ids=[99])

    await sender.notify_admins("<trace> **alert**")

    assert bot.messages == [(99, "<trace> **alert**", None)]
    assert "parse_mode" not in bot.message_kwargs[0]


async def test_sender_answers_callback_with_global_pacing_only() -> None:
    clock = FakeClock()
    bot = FakeBot()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep)

    await sender.answer_callback_query("cb1")
    await sender.answer_callback_query("cb2")

    assert bot.callback_answers == ["cb1", "cb2"]
    assert clock.sleeps == [pytest.approx(1 / 30)]


async def test_typing_uses_global_pacing_without_per_chat_delay() -> None:
    clock = FakeClock()
    bot = FakeBot()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep)

    await sender.send_typing(42)
    await sender.send_typing(42)

    assert bot.actions == [(42, "typing"), (42, "typing")]
    assert clock.sleeps == [pytest.approx(1 / 30)]


async def test_sender_downloads_by_file_id_and_aborts_at_20_mib() -> None:
    bot = FakeBot()
    sender = TelegramSender(bot)

    assert await sender.download_file("abc", 100) == b"file"
    assert bot.downloaded_paths == ["files/abc"]

    bot.download_data = b"x" * (MAX_TELEGRAM_DOWNLOAD_BYTES + 1)
    with pytest.raises(ValueError, match="download limit"):
        await sender.download_file("oversized", MAX_TELEGRAM_DOWNLOAD_BYTES + 1)
