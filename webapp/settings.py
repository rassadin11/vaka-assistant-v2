"""Runtime configuration for the Telegram Mini App backend."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field

from core.db import DatabaseSettings, database_settings_from_env
from core.queue import DEFAULT_REDIS_CACHE_URL, DEFAULT_REDIS_QUEUE_URL

DEFAULT_WEBAPP_PORT = 8001
DEFAULT_MAX_REQUEST_BODY_BYTES = 8_192


@dataclass(frozen=True, slots=True)
class WebAppSettings:
    """Configuration with secrets deliberately hidden from repr and logs."""

    telegram_bot_token: str = field(repr=False)
    session_secret: str = field(repr=False)
    database_url: str
    redis_cache_url: str
    redis_queue_url: str = DEFAULT_REDIS_QUEUE_URL
    port: int = DEFAULT_WEBAPP_PORT
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES


def settings_from_env() -> WebAppSettings:
    """Load Mini App settings from the environment prepared by Infisical."""

    database_settings: DatabaseSettings = database_settings_from_env()
    telegram_bot_token = _required_env("TELEGRAM_BOT_TOKEN")
    session_secret = _required_env("WEBAPP_SESSION_SECRET")
    if hmac.compare_digest(telegram_bot_token, session_secret):
        raise RuntimeError("WEBAPP_SESSION_SECRET must differ from TELEGRAM_BOT_TOKEN.")
    return WebAppSettings(
        telegram_bot_token=telegram_bot_token,
        session_secret=session_secret,
        database_url=database_settings.database_url,
        redis_cache_url=os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL),
        redis_queue_url=os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL),
        port=int(os.getenv("WEBAPP_PORT", str(DEFAULT_WEBAPP_PORT))),
        max_request_body_bytes=int(
            os.getenv("WEBAPP_MAX_REQUEST_BODY_BYTES", str(DEFAULT_MAX_REQUEST_BODY_BYTES))
        ),
    )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value
