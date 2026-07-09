"""Telegram send helpers with process-local pacing and retry handling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

MAX_TELEGRAM_MESSAGE_CHARS = 4096

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TelegramBot(Protocol):
    """Duck-typed subset of aiogram Bot used by the sender."""

    def send_message(self, chat_id: int, text: str) -> Awaitable[object]:
        """Send a text message."""
        ...

    def send_chat_action(self, chat_id: int, action: str) -> Awaitable[object]:
        """Send a chat action."""
        ...


class SendRateLimiter:
    """Process-local send pacer for Telegram global and per-chat limits."""

    def __init__(
        self,
        *,
        global_per_second: float = 30.0,
        per_chat_per_second: float = 1.0,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        if global_per_second <= 0:
            raise ValueError("global_per_second must be positive.")
        if per_chat_per_second <= 0:
            raise ValueError("per_chat_per_second must be positive.")
        self._global_interval = 1.0 / global_per_second
        self._per_chat_interval = 1.0 / per_chat_per_second
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._lock = asyncio.Lock()
        self._next_global_at = 0.0
        self._next_chat_at: dict[int, float] = {}

    async def wait(self, *, chat_id: int | None = None, apply_per_chat: bool = True) -> None:
        """Wait until a send slot is available and reserve it."""

        async with self._lock:
            now = self._clock()
            ready_at = max(now, self._next_global_at)
            if apply_per_chat and chat_id is not None:
                ready_at = max(ready_at, self._next_chat_at.get(chat_id, 0.0))

            self._next_global_at = ready_at + self._global_interval
            if apply_per_chat and chat_id is not None:
                self._next_chat_at[chat_id] = ready_at + self._per_chat_interval

            delay = ready_at - now

        if delay > 0:
            await self._sleep(delay)


class TelegramSender:
    """Send Telegram messages through an aiogram Bot with pacing and retries."""

    def __init__(
        self,
        bot: TelegramBot,
        *,
        admin_chat_ids: Sequence[int] = (),
        limiter: SendRateLimiter | None = None,
        sleep: Sleep | None = None,
        max_retries: int = 3,
        logger: logging.Logger | None = None,
    ) -> None:
        self._bot = bot
        self._admin_chat_ids = tuple(admin_chat_ids)
        self._limiter = limiter if limiter is not None else SendRateLimiter()
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._max_retries = max_retries
        self._logger = logger if logger is not None else LOGGER

    async def send_message(self, chat_id: int, text: str) -> None:
        """Send a text message, splitting long text into Telegram-sized chunks."""

        for chunk in split_telegram_text(text):
            await self._send_message_chunk(chat_id, chunk)

    async def send_typing(self, chat_id: int) -> None:
        """Send a typing action, paced globally but not by the per-chat message limit."""

        await self._limiter.wait(chat_id=chat_id, apply_per_chat=False)
        try:
            await self._bot.send_chat_action(chat_id=chat_id, action="typing")
        except TelegramAPIError as exc:
            self._logger.warning("telegram typing action failed: %s", exc)

    async def notify_admins(self, text: str) -> None:
        """Send a notification to all configured Telegram admins."""

        for chat_id in self._admin_chat_ids:
            try:
                await self.send_message(chat_id, text)
            except TelegramAPIError as exc:
                self._logger.warning("telegram admin notification failed: %s", exc)

    async def _send_message_chunk(self, chat_id: int, text: str) -> None:
        retries = 0
        while True:
            await self._limiter.wait(chat_id=chat_id, apply_per_chat=True)
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
                return
            except TelegramRetryAfter as exc:
                retries += 1
                if retries > self._max_retries:
                    self._logger.warning("telegram retry limit exceeded: %s", exc)
                    raise
                await self._sleep(float(exc.retry_after))
            except TelegramAPIError as exc:
                self._logger.warning("telegram message send failed: %s", exc)
                raise


def split_telegram_text(text: str) -> list[str]:
    """Split text into chunks accepted by Telegram's message API."""

    if text == "":
        return [""]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > MAX_TELEGRAM_MESSAGE_CHARS:
        split_at = remaining.rfind("\n", 0, MAX_TELEGRAM_MESSAGE_CHARS + 1)
        if split_at <= 0:
            split_at = MAX_TELEGRAM_MESSAGE_CHARS
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    chunks.append(remaining)
    return chunks
