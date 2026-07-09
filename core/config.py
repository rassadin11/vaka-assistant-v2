"""Shared service configuration helpers."""

from __future__ import annotations

import os

from core.secrets import EnvSecretsProvider, SecretNotFoundError, SecretsProvider


def admin_ids_from_env(raw_value: str | None = None) -> tuple[int, ...]:
    """Parse comma-separated Telegram admin chat ids."""

    value = os.getenv("ADMIN_TELEGRAM_IDS", "") if raw_value is None else raw_value
    if not value:
        return ()
    return tuple(int(part.strip()) for part in value.split(",") if part.strip())


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
