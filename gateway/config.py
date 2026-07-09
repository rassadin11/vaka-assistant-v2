"""Gateway configuration loaded from environment and local secret providers."""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.queue import RedisSettings, redis_settings_from_env
from core.secrets import EnvSecretsProvider, SecretNotFoundError, SecretsProvider

DEFAULT_WEBHOOK_SECRET_PATH = "dev-webhook"
DEFAULT_TELEGRAM_WEBHOOK_SECRET_TOKEN = "dev-webhook-secret"
DEFAULT_PORT = 8000


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Configuration for webhook auth, Redis, and Telegram CLI operations."""

    webhook_secret_path: str
    telegram_webhook_secret_token: str
    redis: RedisSettings
    port: int
    public_url: str | None
    admin_ids: tuple[int, ...]


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
    )


def telegram_bot_token(provider: SecretsProvider | None = None) -> str:
    """Read the Telegram bot token from the configured secret provider or environment."""

    local_provider = provider if provider is not None else EnvSecretsProvider()
    return local_provider.get("TELEGRAM_BOT_TOKEN")


def optional_telegram_bot_token(provider: SecretsProvider | None = None) -> str | None:
    """Read the Telegram bot token, returning ``None`` when it is not configured."""

    try:
        return telegram_bot_token(provider)
    except SecretNotFoundError:
        return None


def _admin_ids_from_env(raw_value: str) -> tuple[int, ...]:
    if not raw_value:
        return ()
    return tuple(int(part.strip()) for part in raw_value.split(",") if part.strip())
