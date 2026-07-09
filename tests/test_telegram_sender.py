"""Unit tests for Telegram sender pacing and retry behavior."""

from __future__ import annotations

from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramRetryAfter

from core.telegram_sender import SendRateLimiter, TelegramSender


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
        self.actions: list[tuple[int, str]] = []
        self.callback_answers: list[str] = []
        self.message_outcomes: list[object] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: object | None = None,
    ) -> object:
        self.messages.append((chat_id, text, reply_markup))
        if self.message_outcomes:
            outcome = self.message_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return object()

    async def send_chat_action(self, chat_id: int, action: str) -> object:
        self.actions.append((chat_id, action))
        return object()

    async def answer_callback_query(self, callback_query_id: str) -> object:
        self.callback_answers.append(callback_query_id)
        return object()


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
    assert clock.sleeps == [2.0]


async def test_sender_splits_long_messages() -> None:
    clock = FakeClock()
    bot = FakeBot()
    limiter = SendRateLimiter(clock=clock, sleep=clock.sleep)
    sender = TelegramSender(bot, limiter=limiter, sleep=clock.sleep)
    text = ("a" * 4096) + "\n" + ("b" * 10)

    await sender.send_message(42, text)

    assert bot.messages == [(42, "a" * 4096, None), (42, "b" * 10, None)]
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
