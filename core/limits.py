"""Fail-open Redis accounting and reads for user tariff limits."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from core.spend import daily_budget_rub, spend_key
from core.time_keys import local_date_key

MESSAGE_COUNTER_TTL_SECONDS = 3 * 86_400
DEFAULT_DAILY_MESSAGE_LIMIT = 100
PRO_DAILY_MESSAGE_LIMIT = 200
DAILY_VOICE_MINUTES_LIMIT = 10
MONTHLY_PDF_PAGES_LIMIT = 200

LOGGER = logging.getLogger(__name__)


class LimitsRedis(Protocol):
    """Redis commands used by tariff limit counters and snapshots."""

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...

    def incr(self, name: str) -> Awaitable[int]: ...

    def expire(self, name: str, seconds: int) -> Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class LimitUsage:
    """The used and allowed values for one tariff-limit axis."""

    used: int | Decimal
    limit: int | Decimal


@dataclass(frozen=True, slots=True)
class LimitsSnapshot:
    """A read-only view of all tariff counters for one user."""

    messages: LimitUsage
    budget_rub: LimitUsage
    voice_minutes: LimitUsage
    pdf_pages: LimitUsage


def daily_message_limit(plan: str) -> int:
    """Return the configured daily message allowance for a user plan."""

    if plan == "pro":
        return PRO_DAILY_MESSAGE_LIMIT
    return _daily_default_message_limit()


def message_key(user_id: UUID, timezone: str, current: datetime | None = None) -> str:
    """Build today's message counter key using the user's local calendar date."""

    local_current = current or datetime.now(ZoneInfo(timezone))
    return f"msg:{user_id}:{local_date_key(timezone, local_current)}"


async def message_limit_reached(
    queue_redis: LimitsRedis, user_id: UUID, plan: str, timezone: str
) -> bool:
    """Return whether today's message allowance is consumed, failing open on Redis errors."""

    try:
        value = await queue_redis.get(message_key(user_id, timezone))
        return _as_int(value) >= daily_message_limit(plan)
    except Exception:
        LOGGER.warning("daily message limit read failed", exc_info=True)
        return False


async def add_message(queue_redis: LimitsRedis, user_id: UUID, timezone: str) -> None:
    """Record a message entering the agent loop without blocking on Redis failures."""

    key = message_key(user_id, timezone)
    try:
        value = await queue_redis.incr(key)
        if value == 1:
            await queue_redis.expire(key, MESSAGE_COUNTER_TTL_SECONDS)
    except Exception:
        LOGGER.warning("daily message increment failed", exc_info=True)


async def limits_snapshot(
    queue_redis: LimitsRedis, user_id: UUID, plan: str, timezone: str
) -> LimitsSnapshot:
    """Read all tariff counters without modifying Redis, returning zeroes on failure."""

    try:
        messages, spent_rub, voice_minutes, pdf_pages = await _read_snapshot_values(
            queue_redis, user_id, timezone
        )
    except Exception:
        LOGGER.warning("tariff limits snapshot read failed", exc_info=True)
        messages = 0
        spent_rub = Decimal(0)
        voice_minutes = 0
        pdf_pages = 0
    return LimitsSnapshot(
        messages=LimitUsage(messages, daily_message_limit(plan)),
        budget_rub=LimitUsage(spent_rub, daily_budget_rub(plan)),
        voice_minutes=LimitUsage(voice_minutes, DAILY_VOICE_MINUTES_LIMIT),
        pdf_pages=LimitUsage(pdf_pages, MONTHLY_PDF_PAGES_LIMIT),
    )


async def _read_snapshot_values(
    queue_redis: LimitsRedis, user_id: UUID, timezone: str
) -> tuple[int, Decimal, int, int]:
    current = datetime.now(UTC)
    messages = _as_int(await queue_redis.get(message_key(user_id, timezone, current)))
    spent_rub = _as_decimal(await queue_redis.get(spend_key(user_id, timezone, current)))
    voice_minutes = _as_int(await queue_redis.get(_voice_minutes_key(user_id, current)))
    pdf_pages = _as_int(await queue_redis.get(_pdf_pages_key(user_id, current)))
    return messages, spent_rub, voice_minutes, pdf_pages


def _voice_minutes_key(user_id: UUID, current: datetime) -> str:
    return f"stt_min:{user_id}:{current:%Y%m%d}"


def _pdf_pages_key(user_id: UUID, current: datetime) -> str:
    return f"doc_pages:{user_id}:{current:%Y%m}"


def _daily_default_message_limit() -> int:
    value = os.getenv("DAILY_MESSAGE_LIMIT_DEFAULT")
    if value is None or not value.strip():
        return DEFAULT_DAILY_MESSAGE_LIMIT
    try:
        parsed = int(value)
    except ValueError:
        LOGGER.warning("invalid DAILY_MESSAGE_LIMIT_DEFAULT; using default")
        return DEFAULT_DAILY_MESSAGE_LIMIT
    return parsed


def _as_int(value: str | bytes | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return int(value)


def _as_decimal(value: str | bytes | None) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return Decimal(value)
