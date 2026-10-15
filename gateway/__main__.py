"""Command line entry points for the Telegram gateway."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, NoReturn

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, Update, WebAppInfo
from redis.asyncio import Redis

from core.logging_setup import setup_logging
from core.telegram_sender import TelegramSender
from gateway.app import ALLOWED_UPDATES, handle_update
from gateway.config import config_from_env, optional_telegram_bot_token, telegram_bot_token

# Liveness of the polling gateway, written to Redis for the container
# healthcheck (plan/small-profile.md section 6, task S9). The polling deployment
# serves no HTTP, so "the process exists" is all a healthcheck could otherwise
# see -- and that is exactly the state that stays green while Telegram answers
# 409 Conflict because a webhook is still registered on the token.
POLLING_HEARTBEAT_KEY = "gw:polling:heartbeat"
POLLING_HEARTBEAT_INTERVAL_SECONDS = 30.0
POLLING_HEARTBEAT_TTL_SECONDS = 90


def main() -> NoReturn:
    """Run the gateway app or a Telegram management command."""

    setup_logging("gateway")
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "serve":
        config = config_from_env()
        uvicorn.run("gateway.app:create_app", factory=True, host="0.0.0.0", port=config.port)
        raise SystemExit(0)
    if command == "polling":
        asyncio.run(_run_polling())
        raise SystemExit(0)
    if command == "set-webhook":
        asyncio.run(_set_webhook())
        raise SystemExit(0)
    if command == "delete-webhook":
        asyncio.run(_delete_webhook(drop_pending="--drop-pending" in sys.argv[2:]))
        raise SystemExit(0)
    if command == "set-menu-button":
        asyncio.run(_set_menu_button())
        raise SystemExit(0)
    if command == "set-my-commands":
        asyncio.run(_set_my_commands())
        raise SystemExit(0)
    raise SystemExit(f"unknown gateway command: {command}")


async def _run_polling() -> None:
    config = config_from_env()
    token = optional_telegram_bot_token()
    if token is None:
        logging.getLogger(__name__).warning("TELEGRAM_BOT_TOKEN is not configured; polling exits")
        return

    bot = Bot(token)
    sender = TelegramSender(bot, admin_chat_ids=config.admin_ids)
    queue_redis = Redis.from_url(config.redis.queue_url, decode_responses=True)
    cache_redis = Redis.from_url(config.redis.cache_url, decode_responses=True)

    async def on_update(update_dict: dict[str, Any]) -> None:
        await handle_update(
            update_dict,
            queue_redis=queue_redis,
            cache_redis=cache_redis,
            send_user_message=sender.send_message,
            rate_limit_per_minute=config.rate_limit_per_minute,
            rate_limit_burst=config.rate_limit_burst,
        )

    dispatcher = build_polling_dispatcher(on_update)
    heartbeat = asyncio.create_task(_publish_polling_heartbeat(bot, cache_redis))
    try:
        # pre_checkout_query will be added here when payments are implemented.
        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        await queue_redis.aclose()
        await cache_redis.aclose()
        await bot.session.close()


def build_polling_dispatcher(
    on_update: Callable[[dict[str, Any]], Awaitable[None]],
) -> Dispatcher:
    """Route every allowed update type into the shared webhook/polling path.

    ``Dispatcher`` registers its own router-propagation handler on the raw
    ``update`` observer in its constructor, so a handler added there after it is
    never reached: every update ends as "not handled" and is silently dropped.
    The handler is therefore attached to the event observer of each allowed
    update type, and aiogram injects the full ``Update`` as ``event_update``.

    The update is handed on in Bot API shape, as the webhook receives it:
    ``by_alias`` keeps ``from`` instead of aiogram's ``from_user`` field name,
    without which every message looks sender-less and is ignored.
    """

    dispatcher = Dispatcher()

    async def forward(_event: object, event_update: Update) -> None:
        await on_update(event_update.model_dump(mode="json", exclude_none=True, by_alias=True))

    for update_type in ALLOWED_UPDATES:
        dispatcher.observers[update_type].register(forward)
    return dispatcher


async def _publish_polling_heartbeat(bot: Bot, redis: Redis) -> None:
    """Refresh the liveness key while polling can actually receive updates.

    The check is ``getWebhookInfo``: it proves the token still works and that no
    webhook is registered on it. Both matter, because ``getUpdates`` fails with
    409 Conflict for as long as a webhook exists, and aiogram keeps retrying
    that in the background -- a container that looks perfectly alive while no
    message reaches the queue. Any failure simply stops the refresh and lets the
    key expire, so the healthcheck reports the outage instead of the gateway
    deciding on its own to die.
    """

    logger = logging.getLogger(__name__)
    while True:
        try:
            info = await bot.get_webhook_info()
            if info.url:
                logger.warning("webhook still registered while polling: %s", info.url)
            else:
                await redis.set(
                    POLLING_HEARTBEAT_KEY,
                    str(int(time.time())),
                    ex=POLLING_HEARTBEAT_TTL_SECONDS,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("polling heartbeat failed")
        await asyncio.sleep(POLLING_HEARTBEAT_INTERVAL_SECONDS)


async def _set_webhook() -> None:
    config = config_from_env()
    public_url = config.public_url or os.getenv("TELEGRAM_PUBLIC_URL")
    if public_url is None:
        raise RuntimeError("PUBLIC_URL or TELEGRAM_PUBLIC_URL is required for set-webhook.")

    bot = Bot(telegram_bot_token())
    webhook_url = f"{public_url.rstrip('/')}/webhook/{config.webhook_secret_path}"
    try:
        # pre_checkout_query will be added here when payments are implemented.
        await bot.set_webhook(
            webhook_url,
            secret_token=config.telegram_webhook_secret_token,
            allowed_updates=ALLOWED_UPDATES,
        )
    finally:
        await bot.session.close()


async def _delete_webhook(*, drop_pending: bool = False) -> None:
    """Take the webhook off the token so polling can start on it.

    While a webhook is registered, ``getUpdates`` answers 409 Conflict and
    polling never starts, so this is the first step of moving a token from the
    webhook deployment to the polling one (plan section 4.8).

    Pending updates are kept by default: Telegram holds what it could not
    deliver while the old stack was down, and polling picks those up, so nobody
    who wrote during the switchover is silently ignored. Deduplication lives in
    Redis and does not survive the move, so a message the old stack answered but
    failed to acknowledge can be answered twice -- ``--drop-pending`` trades that
    away for losing the backlog instead.
    """

    bot = Bot(telegram_bot_token())
    try:
        await bot.delete_webhook(drop_pending_updates=drop_pending)
        info = await bot.get_webhook_info()
    finally:
        await bot.session.close()
    logging.getLogger(__name__).info(
        "webhook deleted: url=%r pending=%s dropped=%s",
        info.url,
        info.pending_update_count,
        drop_pending,
    )
    if info.url:
        raise RuntimeError(f"webhook is still registered: {info.url}")


async def _set_menu_button() -> None:
    config = config_from_env()
    public_url = config.public_url
    if public_url is None:
        raise RuntimeError("PUBLIC_URL is required for set-menu-button.")

    bot = Bot(telegram_bot_token())
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Открыть",
                web_app=WebAppInfo(url=f"{public_url.rstrip('/')}/app/"),
            )
        )
    finally:
        await bot.session.close()


async def _set_my_commands() -> None:
    bot = Bot(telegram_bot_token())
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Начать работу с ассистентом"),  # noqa: RUF001
                BotCommand(command="help", description="Что умеет ассистент"),
                BotCommand(command="feedback", description="Отправить отзыв разработчику"),
                BotCommand(command="timezone", description="Сменить часовой пояс"),
            ]
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    main()
