"""Fail-open Redis accounting for per-user daily spend budgets."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

SPEND_TTL_SECONDS = 3 * 86_400
DEFAULT_USD_RUB_RATE = Decimal("90")
DEFAULT_DAILY_BUDGET_RUB = Decimal("15")
PRO_DAILY_BUDGET_RUB = Decimal("45")

LOGGER = logging.getLogger(__name__)


class SpendRedis(Protocol):
    """Redis commands used for daily spend accounting."""

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...

    def incrbyfloat(self, name: str, amount: float) -> Awaitable[float | str | bytes]: ...

    def expire(self, name: str, seconds: int) -> Awaitable[object]: ...


class BudgetState(StrEnum):
    """The current daily-budget degradation level."""

    OK = "ok"
    NO_BACKGROUND = "no_background"
    SHORT_CONTEXT = "short_context"
    SOFT_REFUSE = "soft_refuse"


async def add_spend(
    queue_redis: SpendRedis,
    user_id: UUID,
    timezone: str,
    cost_usd: Decimal,
) -> None:
    """Record successful usage in RUB without letting Redis failures stop a task."""

    cost_rub = cost_usd * _usd_rub_rate()
    key = spend_key(user_id, timezone)
    try:
        await queue_redis.incrbyfloat(key, float(cost_rub))
        # Unconditional refresh: comparing the float INCRBYFLOAT reply with the
        # exact Decimal would miss the first increment and leave the key immortal.
        await queue_redis.expire(key, SPEND_TTL_SECONDS)
    except Exception:
        LOGGER.warning("daily spend increment failed", exc_info=True)


async def get_spent_rub(queue_redis: SpendRedis, user_id: UUID, timezone: str) -> Decimal:
    """Return today's recorded RUB spend, or zero when Redis is unavailable."""

    try:
        value = await queue_redis.get(spend_key(user_id, timezone))
        return Decimal(0) if value is None else _as_decimal(value)
    except Exception:
        LOGGER.warning("daily spend read failed", exc_info=True)
        return Decimal(0)


def daily_budget_rub(plan: str) -> Decimal:
    """Return the configured daily RUB budget for a user plan."""

    if plan == "pro":
        return PRO_DAILY_BUDGET_RUB
    return _daily_default_budget()


def budget_state(spent: Decimal, budget: Decimal) -> BudgetState:
    """Map spend-to-budget ratio to the applicable degradation step."""

    if budget <= 0 or spent >= budget * Decimal("1.5"):
        return BudgetState.SOFT_REFUSE
    if spent >= budget * Decimal("1.2"):
        return BudgetState.SHORT_CONTEXT
    if spent >= budget:
        return BudgetState.NO_BACKGROUND
    return BudgetState.OK


def spend_key(user_id: UUID, timezone: str) -> str:
    """Build today's spend key using the user's local calendar date."""

    return f"spend_rub:{user_id}:{datetime.now(ZoneInfo(timezone)):%Y%m%d}"


def _usd_rub_rate() -> Decimal:
    return _decimal_from_env("USD_RUB_RATE", DEFAULT_USD_RUB_RATE)


def _daily_default_budget() -> Decimal:
    return _decimal_from_env("DAILY_BUDGET_RUB_DEFAULT", DEFAULT_DAILY_BUDGET_RUB)


def _decimal_from_env(name: str, default: Decimal) -> Decimal:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return Decimal(value)
    except InvalidOperation:
        LOGGER.warning("invalid %s; using default", name)
        return default


def _as_decimal(value: float | str | bytes | Decimal) -> Decimal:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return Decimal(str(value))
