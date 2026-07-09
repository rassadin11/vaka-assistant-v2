"""Command line entry point for the queue worker."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import NoReturn

import asyncpg
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis

from core.config import admin_ids_from_env, optional_telegram_bot_token
from core.db import create_service_pool
from core.queue import redis_settings_from_env
from core.telegram_sender import TelegramSender
from worker.app import NotifyAdminCallback, SendReplyCallback, SendTypingCallback, Worker
from worker.config import config_from_env
from worker.onboarding import AnswerCallback, OnboardingProcessor, SendCallback
from worker.processor import EchoProcessor

ButtonRows = list[list[tuple[str, str]]]
RichSendCallback = SendCallback


def main() -> NoReturn:
    """Run the worker process."""

    handler = logging.StreamHandler()
    handler.addFilter(_TraceIdFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s trace_id=%(trace_id)s %(name)s: %(message)s")
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        force=True,
    )
    asyncio.run(_run())
    raise SystemExit(0)


async def _run() -> None:
    settings = redis_settings_from_env()
    config = config_from_env()
    admin_ids = admin_ids_from_env()
    service_pool = await _create_service_pool_or_fail()
    queue_redis = Redis.from_url(settings.queue_url, decode_responses=True)
    cache_redis = Redis.from_url(settings.cache_url, decode_responses=True)
    bot: Bot | None = None
    token = optional_telegram_bot_token()
    send_reply: SendReplyCallback
    send_typing: SendTypingCallback
    notify_admin: NotifyAdminCallback
    rich_send: RichSendCallback
    answer_callback: AnswerCallback
    if token is None:
        send_reply = _log_reply
        send_typing = _log_typing
        notify_admin = _log_admin
        rich_send = _log_rich_send
        answer_callback = _log_callback_answer
    else:
        bot = Bot(token)
        sender = TelegramSender(bot, admin_chat_ids=admin_ids)
        send_reply = sender.send_message
        send_typing = sender.send_typing
        notify_admin = sender.notify_admins
        rich_send = _rich_send_adapter(sender)
        answer_callback = sender.answer_callback_query
    processor = OnboardingProcessor(
        service_pool=service_pool,
        cache_redis=cache_redis,
        inner=EchoProcessor(),
        send=rich_send,
        answer_callback=answer_callback,
        notify_admin=notify_admin,
        admin_ids=admin_ids,
    )
    worker = Worker(
        queue_redis=queue_redis,
        cache_redis=cache_redis,
        processor=processor,
        send_reply=send_reply,
        send_typing=send_typing,
        notify_admin=notify_admin,
        config=config,
    )
    _install_signal_handlers(worker)
    try:
        await worker.run()
    except KeyboardInterrupt:
        worker.request_stop()
    finally:
        await queue_redis.aclose()
        await cache_redis.aclose()
        await service_pool.close()
        if bot is not None:
            await bot.session.close()


async def _create_service_pool_or_fail() -> asyncpg.Pool:
    try:
        return await create_service_pool()
    except Exception:
        logging.getLogger(__name__).critical(
            "worker startup failed: Postgres service database is unreachable",
            exc_info=True,
        )
        raise


def _rich_send_adapter(sender: TelegramSender) -> RichSendCallback:
    async def send(chat_id: int, text: str, buttons: ButtonRows | None = None) -> None:
        reply_markup = _inline_keyboard(buttons) if buttons is not None else None
        await sender.send_message(chat_id, text, reply_markup=reply_markup)

    return send


def _inline_keyboard(buttons: ButtonRows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=button_text, callback_data=callback_data)
                for button_text, callback_data in row
            ]
            for row in buttons
        ]
    )


def _install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, worker.request_stop)
        except NotImplementedError:
            signal.signal(signal_number, lambda _signum, _frame: worker.request_stop())


async def _log_reply(chat_id: int, text: str) -> None:
    logging.getLogger(__name__).info("reply chat_id=%s text=%s", chat_id, text)


async def _log_typing(chat_id: int) -> None:
    logging.getLogger(__name__).info("typing chat_id=%s", chat_id)


async def _log_admin(text: str) -> None:
    logging.getLogger(__name__).warning("admin notification: %s", text)


async def _log_rich_send(chat_id: int, text: str, buttons: ButtonRows | None = None) -> None:
    logging.getLogger(__name__).info(
        "reply chat_id=%s text=%s buttons=%s",
        chat_id,
        text,
        buttons,
    )


async def _log_callback_answer(callback_query_id: str) -> None:
    logging.getLogger(__name__).info("answer callback_query_id=%s", callback_query_id)


class _TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = "-"
        return True


if __name__ == "__main__":
    main()
