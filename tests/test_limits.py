"""Unit tests for tariff-limit message accounting and snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from core.limits import (
    DAILY_VOICE_MINUTES_LIMIT,
    LIMIT_NOTICE_DAILY_TTL_SECONDS,
    LIMIT_NOTICE_MONTHLY_TTL_SECONDS,
    MESSAGE_COUNTER_TTL_SECONDS,
    MONTHLY_PDF_PAGES_LIMIT,
    LimitAxis,
    add_message,
    claim_limit_notice,
    daily_message_limit,
    limits_snapshot,
    message_key,
    message_limit_reached,
)
from core.spend import daily_budget_rub, spend_key

USER_ID = UUID("018f0000-0000-7000-8000-000000000001")


class FakeRedis:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.expire_calls: list[tuple[str, int]] = []
        self.set_calls: list[tuple[str, int | None, bool]] = []

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def incr(self, name: str) -> int:
        value = int(self.values.get(name, "0")) + 1
        self.values[name] = str(value)
        return value

    async def expire(self, name: str, seconds: int) -> bool:
        self.expire_calls.append((name, seconds))
        return True

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        del value
        self.set_calls.append((name, ex, nx))
        if nx and name in self.values:
            return False
        self.values[name] = "1"
        return True


class FailingRedis(FakeRedis):
    async def get(self, name: str) -> str | None:
        raise ConnectionError(name)

    async def incr(self, name: str) -> int:
        raise ConnectionError(name)

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        del value, ex, nx
        raise ConnectionError(name)


def test_daily_message_limit_uses_plan_mapping_and_default_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_MESSAGE_LIMIT_DEFAULT", "120")

    assert daily_message_limit("trial") == 120
    assert daily_message_limit("standard") == 120
    assert daily_message_limit("pro") == 200


def test_message_key_uses_the_users_local_date(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            assert tz is not None
            return datetime(2026, 7, 11, 18, tzinfo=UTC).astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr("core.limits.datetime", FixedDatetime)

    assert message_key(USER_ID, "Asia/Vladivostok") == f"msg:{USER_ID}:20260712"


async def test_add_message_increments_and_sets_ttl_only_on_first_increment() -> None:
    redis = FakeRedis()
    key = message_key(USER_ID, "Europe/Moscow")

    await add_message(redis, USER_ID, "Europe/Moscow")
    await add_message(redis, USER_ID, "Europe/Moscow")

    assert redis.values[key] == "2"
    assert redis.expire_calls == [(key, MESSAGE_COUNTER_TTL_SECONDS)]


async def test_message_counter_redis_failures_are_open() -> None:
    redis = FailingRedis()

    assert not await message_limit_reached(redis, USER_ID, "trial", "Europe/Moscow")
    await add_message(redis, USER_ID, "Europe/Moscow")
    snapshot = await limits_snapshot(redis, USER_ID, "trial", "Europe/Moscow")

    assert snapshot.messages.used == 0
    assert snapshot.budget_rub.used == Decimal(0)
    assert snapshot.voice_minutes.used == 0
    assert snapshot.pdf_pages.used == 0


async def test_limits_snapshot_reads_all_existing_counter_key_formats() -> None:
    timezone = "Asia/Almaty"
    values = {
        message_key(USER_ID, timezone): "7",
        spend_key(USER_ID, timezone): "12.5",
        f"stt_min:{USER_ID}:{datetime.now(UTC):%Y%m%d}": "3",
        f"doc_pages:{USER_ID}:{datetime.now(UTC):%Y%m}": "44",
    }

    snapshot = await limits_snapshot(FakeRedis(values), USER_ID, "trial", timezone)

    assert snapshot.messages.used == 7
    assert snapshot.messages.limit == 100
    assert snapshot.budget_rub.used == Decimal("12.5")
    assert snapshot.budget_rub.limit == daily_budget_rub("trial")
    assert snapshot.voice_minutes.used == 3
    assert snapshot.voice_minutes.limit == DAILY_VOICE_MINUTES_LIMIT
    assert snapshot.pdf_pages.used == 44
    assert snapshot.pdf_pages.limit == MONTHLY_PDF_PAGES_LIMIT


async def test_limit_notice_claim_is_deduplicated_per_axis_period_and_uses_fixed_ttls() -> None:
    redis = FakeRedis()

    assert await claim_limit_notice(redis, LimitAxis.MESSAGES, USER_ID, "20260711")
    assert not await claim_limit_notice(redis, LimitAxis.MESSAGES, USER_ID, "20260711")
    assert await claim_limit_notice(redis, LimitAxis.BUDGET, USER_ID, "20260711")
    assert await claim_limit_notice(redis, LimitAxis.PDF, USER_ID, "202607")

    assert redis.set_calls == [
        (f"limit_notice:messages:{USER_ID}:20260711", LIMIT_NOTICE_DAILY_TTL_SECONDS, True),
        (f"limit_notice:messages:{USER_ID}:20260711", LIMIT_NOTICE_DAILY_TTL_SECONDS, True),
        (f"limit_notice:budget:{USER_ID}:20260711", LIMIT_NOTICE_DAILY_TTL_SECONDS, True),
        (f"limit_notice:pdf:{USER_ID}:202607", LIMIT_NOTICE_MONTHLY_TTL_SECONDS, True),
    ]


async def test_limit_notice_claim_fails_open_when_redis_is_unavailable() -> None:
    assert not await claim_limit_notice(FailingRedis(), LimitAxis.VOICE, USER_ID, "20260711")
