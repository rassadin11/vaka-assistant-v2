"""Telegram send helpers with process-local pacing and retry handling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Buffer, Callable, Sequence
from io import BytesIO
from typing import Protocol

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    ForceReply,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from core.telegram_format import markdown_to_telegram_html

MAX_TELEGRAM_MESSAGE_CHARS = 4096
MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
TelegramReplyMarkup = ForceReply | InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove


class TelegramBot(Protocol):
    """Duck-typed subset of aiogram Bot used by the sender."""

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: TelegramReplyMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Awaitable[object]:
        """Send a text message."""
        ...

    def send_chat_action(self, chat_id: int, action: str) -> Awaitable[object]:
        """Send a chat action."""
        ...

    def send_photo(
        self,
        chat_id: int | str,
        photo: BufferedInputFile,
        *,
        caption: str | None = None,
    ) -> Awaitable[object]:
        """Send a multipart photo."""
        ...

    def answer_callback_query(self, callback_query_id: str) -> Awaitable[object]:
        """Answer a callback query."""
        ...

    def get_file(self, file_id: str) -> Awaitable[object]:
        """Resolve a Telegram file id to its remote path."""
        ...

    def download_file(
        self,
        file_path: str,
        destination: BytesIO,
        *,
        timeout: int = 30,
    ) -> Awaitable[object]:
        """Download a file into the supplied byte stream."""
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

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: TelegramReplyMarkup | None = None,
    ) -> None:
        """Send a text message, splitting long text into Telegram-sized chunks."""

        await self._send_text(chat_id, text, reply_markup=reply_markup, format_as_html=True)

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: TelegramReplyMarkup | None = None,
        format_as_html: bool,
    ) -> None:
        """Send split text, optionally formatting each raw chunk as Telegram HTML."""

        chunks = split_telegram_text(text)
        for index, chunk in enumerate(chunks):
            chunk_markup = reply_markup if index == len(chunks) - 1 else None
            await self._send_message_chunk(
                chat_id,
                chunk,
                reply_markup=chunk_markup,
                format_as_html=format_as_html,
            )

    async def send_photo(self, chat_id: int, photo: bytes, caption: str | None = None) -> None:
        """Send a PNG photo using the same pacing and 429 retry policy as messages."""

        retries = 0
        while True:
            await self._limiter.wait(chat_id=chat_id, apply_per_chat=True)
            try:
                await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=BufferedInputFile(photo, filename="finance.png"),
                    caption=caption,
                )
                return
            except TelegramRetryAfter as exc:
                retries += 1
                if retries > self._max_retries:
                    self._logger.warning("telegram photo retry limit exceeded: %s", exc)
                    raise
                await self._sleep(float(exc.retry_after))
            except TelegramAPIError as exc:
                self._logger.warning("telegram photo send failed: %s", exc)
                raise

    async def download_file(self, file_id: str, max_bytes: int) -> bytes:
        """Download a Telegram file while enforcing the 20 MiB ingest ceiling."""

        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        limit = min(max_bytes, MAX_TELEGRAM_DOWNLOAD_BYTES)
        file = await self._bot.get_file(file_id)
        file_path = getattr(file, "file_path", None)
        if not isinstance(file_path, str) or file_path == "":
            raise ValueError("Telegram getFile response has no file path")
        destination = _LimitedBytesIO(limit)
        await self._bot.download_file(file_path, destination, timeout=30)
        return destination.getvalue()

    async def answer_callback_query(self, callback_query_id: str) -> None:
        """Answer a Telegram callback query without retrying API failures."""

        await self._limiter.wait(apply_per_chat=False)
        try:
            await self._bot.answer_callback_query(callback_query_id=callback_query_id)
        except TelegramAPIError as exc:
            self._logger.warning("telegram callback answer failed: %s", exc)

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
                await self._send_text(chat_id, text, format_as_html=False)
            except TelegramAPIError as exc:
                self._logger.warning("telegram admin notification failed: %s", exc)

    async def _send_message_chunk(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: TelegramReplyMarkup | None = None,
        format_as_html: bool,
    ) -> None:
        retries = 0
        outgoing_text = markdown_to_telegram_html(text) if format_as_html else text
        use_html = format_as_html
        while True:
            await self._limiter.wait(chat_id=chat_id, apply_per_chat=True)
            try:
                if use_html:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=outgoing_text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                else:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=outgoing_text,
                        reply_markup=reply_markup,
                    )
                return
            except TelegramRetryAfter as exc:
                retries += 1
                if retries > self._max_retries:
                    self._logger.warning("telegram retry limit exceeded: %s", exc)
                    raise
                await self._sleep(float(exc.retry_after))
            except TelegramBadRequest as exc:
                # Conversion can both break entity parsing and inflate the chunk
                # past the length limit, so any HTML-mode rejection retries plain.
                if use_html:
                    self._logger.warning(
                        "telegram HTML send rejected; resending plain text: %s", exc
                    )
                    outgoing_text = text
                    use_html = False
                    continue
                self._logger.warning("telegram message send failed: %s", exc)
                raise
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


class _LimitedBytesIO(BytesIO):
    """A write sink that stops aiogram downloads before they exceed a byte cap."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, data: Buffer) -> int:
        if self.tell() + len(memoryview(data)) > self._limit:
            raise ValueError("Telegram file exceeds the configured download limit")
        return super().write(data)
