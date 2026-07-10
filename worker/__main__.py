"""Command line entry point for the queue worker.

Test-only environment variables:
- ``WORKER_PLAIN_ECHO=1`` skips onboarding/Postgres setup and runs the echo
  processor directly.
- ``WORKER_REPLY_STREAM`` records replies to the named Redis stream instead of
  sending them to Telegram or logging them.
- ``WORKER_PLAIN_ECHO_DELAY_SECONDS`` optionally delays each plain-echo
  message, for subprocess reliability tests that need an in-flight message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import replace
from typing import NoReturn, cast

import asyncpg
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis

from core.agent import AgentLoop, AgentLoopConfig
from core.config import admin_ids_from_env, optional_telegram_bot_token
from core.db import create_service_pool
from core.envelope import UpdateEnvelope
from core.llm_openrouter import OpenRouterProvider, openrouter_settings_from_env
from core.llm_resilient import ResilientLLMProvider
from core.model_router import RouteRequest, StaticModelRouter
from core.queue import redis_settings_from_env
from core.secrets import EnvSecretsProvider, SecretNotFoundError
from core.telegram_sender import TelegramSender
from core.tools_dispatch import StaticToolDispatcher
from tools.clock import GET_CURRENT_TIME_DEFINITION, get_current_time
from worker.agent_processor import AgentProcessor
from worker.app import NotifyAdminCallback, SendReplyCallback, SendTypingCallback, Worker
from worker.config import config_from_env
from worker.onboarding import (
    AnswerCallback,
    OnboardingProcessor,
    SendCallback,
)
from worker.onboarding import (
    NotifyAdmin as OnboardingNotifyAdmin,
)
from worker.processor import EchoProcessor, Processor

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
    plain_echo = _env_bool("WORKER_PLAIN_ECHO")
    service_pool: asyncpg.Pool | None = None
    if not plain_echo:
        service_pool = await _create_service_pool_or_fail()
    queue_redis = Redis.from_url(settings.queue_url, decode_responses=True)
    cache_redis = Redis.from_url(settings.cache_url, decode_responses=True)
    bot: Bot | None = None
    token = optional_telegram_bot_token()
    reply_stream = os.getenv("WORKER_REPLY_STREAM")
    send_reply: SendReplyCallback
    send_typing: SendTypingCallback
    notify_admin: NotifyAdminCallback
    rich_send: RichSendCallback
    answer_callback: AnswerCallback
    if reply_stream is not None:
        send_reply = _stream_reply(queue_redis, reply_stream)
        send_typing = _log_typing
        notify_admin = _stream_admin(queue_redis, reply_stream)
        rich_send = _rich_send_from_reply(send_reply)
        answer_callback = _log_callback_answer
    elif token is None:
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
    processor: Processor
    if plain_echo:
        processor = TestableEchoProcessor()
        logging.getLogger(__name__).info("inner processor active: plain echo")
    else:
        if service_pool is None:
            raise RuntimeError("service pool is required outside WORKER_PLAIN_ECHO mode")
        inner = _active_inner_processor(queue_redis, send_reply, notify_admin)
        processor = OnboardingProcessor(
            service_pool=service_pool,
            cache_redis=cache_redis,
            inner=inner,
            send=rich_send,
            answer_callback=answer_callback,
            notify_admin=cast(OnboardingNotifyAdmin, notify_admin),
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
        if service_pool is not None:
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


def _rich_send_from_reply(send_reply: SendReplyCallback) -> RichSendCallback:
    async def send(chat_id: int, text: str, buttons: ButtonRows | None = None) -> None:
        del buttons
        await send_reply(chat_id, text)

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


def _stream_reply(redis: Redis, stream: str) -> SendReplyCallback:
    async def send_reply(chat_id: int, text: str) -> None:
        await redis.xadd(stream, {"chat_id": str(chat_id), "text": text})

    return send_reply


def _stream_admin(redis: Redis, stream: str) -> NotifyAdminCallback:
    async def notify_admin(text: str) -> None:
        await redis.xadd(stream, {"chat_id": "0", "text": text, "kind": "admin"})

    return notify_admin


def _env_bool(name: str) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return False
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _active_inner_processor(
    queue_redis: Redis,
    send_reply: SendReplyCallback,
    notify_admin: NotifyAdminCallback,
) -> AgentProcessor | EchoProcessor:
    """Choose the production agent only when its API key can be resolved."""

    try:
        api_key = EnvSecretsProvider().get("OPENROUTER_API_KEY")
    except SecretNotFoundError:
        logging.getLogger(__name__).info("inner processor active: echo (no OpenRouter key)")
        return EchoProcessor()
    if not api_key.strip():
        logging.getLogger(__name__).info("inner processor active: echo (empty OpenRouter key)")
        return EchoProcessor()

    dispatcher = StaticToolDispatcher(
        [GET_CURRENT_TIME_DEFINITION],
        {"get_current_time": get_current_time},
    )
    settings = openrouter_settings_from_env()
    route = StaticModelRouter(settings.model).route(
        RouteRequest(task_type="interactive", user_plan="standard")
    )
    if route.model != settings.model:
        raise RuntimeError("The routed model must match the configured OpenRouter model.")
    agent_config = AgentLoopConfig.from_env()
    agent_config = replace(
        agent_config,
        task_budget_rub=agent_config.task_budget_rub * route.budget_multiplier,
    )
    fallback_model = os.getenv("OPENROUTER_FALLBACK_MODEL", "").strip()
    fallback_provider = (
        OpenRouterProvider(replace(settings, model=fallback_model)) if fallback_model else None
    )
    logging.getLogger(__name__).info("inner processor active: agent")
    return AgentProcessor(
        AgentLoop(
            ResilientLLMProvider(
                OpenRouterProvider(settings),
                queue_redis,
                settings.model,
                fallback_provider=fallback_provider,
                notify_admin=notify_admin,
            ),
            dispatcher,
            agent_config,
        ),
        send=send_reply,
    )


class TestableEchoProcessor:
    """Plain echo processor with test-only poison and delay controls."""

    async def process(self, envelope: UpdateEnvelope) -> str | None:
        payload = envelope.payload
        text = payload.get("text")
        if text == "__poison__":
            raise RuntimeError("test poison message")
        delay_seconds = float(os.getenv("WORKER_PLAIN_ECHO_DELAY_SECONDS", "0"))
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        return text if isinstance(text, str) else None


class _TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = "-"
        return True


if __name__ == "__main__":
    main()
