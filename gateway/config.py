"""Gateway configuration loaded from environment and local secret providers."""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.config import (
    admin_ids_from_env,
    optional_telegram_bot_token,
    telegram_bot_token,
)
from core.queue import RedisSettings, redis_settings_from_env
from core.rate_limit import DEFAULT_RATE_LIMIT_BURST, DEFAULT_RATE_LIMIT_PER_MINUTE

DEFAULT_WEBHOOK_SECRET_PATH = "dev-webhook"
DEFAULT_TELEGRAM_WEBHOOK_SECRET_TOKEN = "dev-webhook-secret"
DEFAULT_PORT = 8000

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_TELEGRAM_WEBHOOK_SECRET_TOKEN",
    "DEFAULT_WEBHOOK_SECRET_PATH",
    "GatewayConfig",
    "config_from_env",
    "optional_telegram_bot_token",
    "telegram_bot_token",
]


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Configuration for webhook auth, Redis, and Telegram CLI operations."""

    webhook_secret_path: str
    telegram_webhook_secret_token: str
    redis: RedisSettings
    port: int
    public_url: str | None
    admin_ids: tuple[int, ...]
    rate_limit_per_minute: int
    rate_limit_burst: int


def config_from_env() -> GatewayConfig:
    """Load gateway config with harmless local development defaults."""

    return GatewayConfig(
        webhook_secret_path=os.getenv("WEBHOOK_SECRET_PATH", DEFAULT_WEBHOOK_SECRET_PATH),
        telegram_webhook_secret_token=os.getenv(
            "TELEGRAM_WEBHOOK_SECRET_TOKEN",
            DEFAULT_TELEGRAM_WEBHOOK_SECRET_TOKEN,
        ),
        redis=redis_settings_from_env(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        public_url=os.getenv("PUBLIC_URL"),
        admin_ids=_admin_ids_from_env(os.getenv("ADMIN_TELEGRAM_IDS", "")),
        rate_limit_per_minute=int(
            os.getenv("RATE_LIMIT_PER_MINUTE", str(DEFAULT_RATE_LIMIT_PER_MINUTE))
        ),
        rate_limit_burst=int(os.getenv("RATE_LIMIT_BURST", str(DEFAULT_RATE_LIMIT_BURST))),
    )


def _admin_ids_from_env(raw_value: str) -> tuple[int, ...]:
    return admin_ids_from_env(raw_value)
