"""Command line entry points for the Telegram gateway."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import NoReturn

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from redis.asyncio import Redis

from core.telegram_sender import TelegramSender
from gateway.app import ALLOWED_UPDATES, handle_update
from gateway.config import config_from_env, optional_telegram_bot_token, telegram_bot_token


def main() -> NoReturn:
    """Run the gateway app or a Telegram management command."""

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
    raise SystemExit(f"unknown gateway command: {command}")


async def _run_polling() -> None:
    config = config_from_env()
    token = optional_telegram_bot_token()
    if token is None:
        logging.getLogger(__name__).warning("TELEGRAM_BOT_TOKEN is not configured; polling exits")
        return

    bot = Bot(token)
    sender = TelegramSender(bot, admin_chat_ids=config.admin_ids)
    dispatcher = Dispatcher()
    queue_redis = Redis.from_url(config.redis.queue_url, decode_responses=True)
    cache_redis = Redis.from_url(config.redis.cache_url, decode_responses=True)

    async def on_update(update: Update) -> None:
        await handle_update(
            update.model_dump(mode="json", exclude_none=True),
            queue_redis=queue_redis,
            cache_redis=cache_redis,
            send_user_message=sender.send_message,
            rate_limit_per_minute=config.rate_limit_per_minute,
            rate_limit_burst=config.rate_limit_burst,
        )

    dispatcher.update.register(on_update)
    try:
        # pre_checkout_query will be added here when payments are implemented.
        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        await queue_redis.aclose()
        await cache_redis.aclose()
        await bot.session.close()


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


if __name__ == "__main__":
    main()
