"""Unit tests for Telegram sender pacing and retry behavior."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputRichMessage

from core.telegram_sender import (
    MAX_TELEGRAM_DOWNLOAD_BYTES,
    MAX_TELEGRAM_RICH_MESSAGE_BYTES,
    SendRateLimiter,
    TelegramSender,
)


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
        self.rich_messages: list[tuple[int, InputRichMessage, object | None]] = []
        self.rich_outcomes: list[object] = []

    async def send_rich_message(
        self,
        chat_id: int,
        rich_message: InputRichMessage,
        **kwargs: Any,
    ) -> object:
        self.rich_messages.append((chat_id, rich_message, kwargs.get("reply_markup")))
        if self.rich_outcomes:
            outcome = self.rich_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return object()

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
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep, use_rich_messages=False)

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
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep, use_rich_messages=False)
    text = ("a" * 4096) + "\n" + ("b" * 10)

    await sender.send_message(42, text)

    assert bot.messages == [(42, "a" * 4096, None), (42, "b" * 10, None)]
    assert [kwargs["parse_mode"] for kwargs in bot.message_kwargs] == ["HTML", "HTML"]
    assert clock.sleeps == [1.0]


async def test_sender_applies_reply_markup_to_last_chunk_only() -> None:
    clock = FakeClock()
    bot = FakeBot()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep, use_rich_messages=False)
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
    sender = TelegramSender(bot, use_rich_messages=False)

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
    assert bot.rich_messages == []


async def test_sender_sends_short_text_as_single_rich_message() -> None:
    bot = FakeBot()
    sender = TelegramSender(bot)
    markup = object()
    text = "# Heading\n\n| a | b |\n| - | - |\n| 1 | 2 |"

    await sender.send_message(42, text, reply_markup=markup)

    assert len(bot.rich_messages) == 1
    chat_id, rich_message, reply_markup = bot.rich_messages[0]
    assert chat_id == 42
    assert rich_message.markdown == text
    assert rich_message.html is None
    assert rich_message.blocks is None
    assert rich_message.media is None
    assert rich_message.skip_entity_detection is None
    assert reply_markup is markup
    assert bot.messages == []


async def test_sender_falls_back_to_legacy_path_after_rich_rejection() -> None:
    bot = FakeBot()
    bot.rich_outcomes = [TelegramBadRequest(method=cast(Any, None), message="method not found")]
    sender = TelegramSender(bot)
    markup = object()

    await sender.send_message(42, "**bold**", reply_markup=markup)

    assert len(bot.rich_messages) == 1
    assert bot.messages == [(42, "<b>bold</b>", markup)]
    assert bot.message_kwargs[0]["parse_mode"] == "HTML"


async def test_sender_falls_back_to_plain_text_after_rich_and_html_rejection() -> None:
    bot = FakeBot()
    bot.rich_outcomes = [TelegramBadRequest(method=cast(Any, None), message="bad markdown")]
    bot.message_outcomes = [
        TelegramBadRequest(method=cast(Any, None), message="can't parse entities"),
        object(),
    ]
    sender = TelegramSender(bot)

    await sender.send_message(42, "**bold**")

    assert bot.messages == [(42, "<b>bold</b>", None), (42, "**bold**", None)]
    assert "parse_mode" not in bot.message_kwargs[1]


async def test_sender_skips_rich_message_for_oversized_text() -> None:
    clock = FakeClock()
    bot = FakeBot()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep)
    text = "a" * (MAX_TELEGRAM_RICH_MESSAGE_BYTES + 1)

    await sender.send_message(42, text)

    assert bot.rich_messages == []
    assert [len(sent) for _chat_id, sent, _markup in bot.messages] == [
        4096,
        4096,
        4096,
        4096,
        4096,
        4096,
        4096,
        4096,
        MAX_TELEGRAM_RICH_MESSAGE_BYTES + 1 - 8 * 4096,
    ]
    assert {kwargs["parse_mode"] for kwargs in bot.message_kwargs} == {"HTML"}


async def test_sender_counts_rich_message_limit_in_utf8_bytes() -> None:
    bot = FakeBot()
    sender = TelegramSender(bot)
    # Cyrillic is two bytes per character, so this fits by character count but
    # not by the byte budget.
    text = "я" * (MAX_TELEGRAM_RICH_MESSAGE_BYTES // 2 + 1)

    await sender.send_message(42, text)

    assert bot.rich_messages == []
    assert bot.messages != []


async def test_sender_retries_rich_message_after_429_before_any_fallback() -> None:
    clock = FakeClock()
    bot = FakeBot()
    bot.rich_outcomes = [
        TelegramRetryAfter(method=cast(Any, None), message="retry", retry_after=2),
        object(),
    ]
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep)

    await sender.send_message(42, "hello")

    assert [text.markdown for _chat_id, text, _markup in bot.rich_messages] == ["hello", "hello"]
    assert bot.messages == []
    assert clock.sleeps == [2.0]


async def test_sender_raises_after_rich_retry_limit() -> None:
    clock = FakeClock()
    bot = FakeBot()
    bot.rich_outcomes = [
        TelegramRetryAfter(method=cast(Any, None), message="retry", retry_after=1) for _ in range(5)
    ]
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep, max_retries=2)

    with pytest.raises(TelegramRetryAfter):
        await sender.send_message(42, "hello")

    assert len(bot.rich_messages) == 3
    assert bot.messages == []


async def test_sender_disables_rich_messages_on_request() -> None:
    bot = FakeBot()
    sender = TelegramSender(bot, use_rich_messages=False)

    await sender.send_message(42, "**bold**")

    assert bot.rich_messages == []
    assert bot.messages == [(42, "<b>bold</b>", None)]
    assert bot.message_kwargs[0]["parse_mode"] == "HTML"


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
