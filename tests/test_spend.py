"""Unit tests for daily RUB spend accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from core.spend import (
    SPEND_TTL_SECONDS,
    BudgetState,
    add_spend,
    budget_state,
    daily_budget_rub,
    get_spent_rub,
    spend_key,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, Decimal] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, name: str) -> str | None:
        value = self.values.get(name)
        return None if value is None else str(value)

    async def incrbyfloat(self, name: str, amount: float | Decimal) -> Decimal:
        value = self.values.get(name, Decimal(0)) + Decimal(str(amount))
        self.values[name] = value
        return value

    async def expire(self, name: str, seconds: int) -> bool:
        self.expiries[name] = seconds
        return True


class FailingRedis(FakeRedis):
    async def get(self, name: str) -> str | None:
        raise ConnectionError(name)

    async def incrbyfloat(self, name: str, amount: float | Decimal) -> Decimal:
        raise ConnectionError(f"{name}:{amount}")


USER_ID = UUID("018f0000-0000-7000-8000-000000000001")


def test_budget_state_uses_inclusive_degradation_boundaries() -> None:
    budget = Decimal("15")

    assert budget_state(Decimal("14.999"), budget) is BudgetState.OK
    assert budget_state(Decimal("15"), budget) is BudgetState.NO_BACKGROUND
    assert budget_state(Decimal("18"), budget) is BudgetState.SHORT_CONTEXT
    assert budget_state(Decimal("22.5"), budget) is BudgetState.SOFT_REFUSE


def test_daily_budget_uses_plan_mapping_and_default_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_RUB_DEFAULT", "20")

    assert daily_budget_rub("trial") == Decimal("20")
    assert daily_budget_rub("standard") == Decimal("20")
    assert daily_budget_rub("pro") == Decimal("45")


async def test_add_spend_uses_local_date_and_sets_ttl_on_first_increment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            assert tz is not None
            return datetime(2026, 7, 11, 18, tzinfo=UTC).astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr("core.spend.datetime", FixedDatetime)
    redis = FakeRedis()

    await add_spend(redis, USER_ID, "Asia/Vladivostok", Decimal("0.1"))

    key = f"spend_rub:{USER_ID}:20260712"
    assert redis.values[key] == Decimal("9.0")
    assert redis.expiries == {key: SPEND_TTL_SECONDS}
    assert spend_key(USER_ID, "Asia/Vladivostok") == key


async def test_spend_redis_failures_are_open() -> None:
    redis = FailingRedis()

    await add_spend(redis, USER_ID, "Europe/Moscow", Decimal("1"))

    assert await get_spent_rub(redis, USER_ID, "Europe/Moscow") == Decimal(0)
