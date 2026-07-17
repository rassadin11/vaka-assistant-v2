"""Offline coverage for the stage-4 finance tool handlers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from core.context import TaskContext
from tools.finance import (
    AddTransactionArgs,
    QueryTransactionsArgs,
    SetBudgetArgs,
    _add_transaction,
    _get_budget_status,
    _query_transactions,
    _set_budget,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.budgets: dict[str, Decimal] = {}

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            return "SELECT 1"
        if "INSERT INTO transactions" in query:
            self.transactions.append(
                {
                    "amount": args[1],
                    "direction": args[2],
                    "category": args[3],
                    "currency": "RUB",
                    "description": args[4],
                    "ts": args[5],
                }
            )
            return "INSERT 0 1"
        if "INSERT INTO budgets" in query:
            self.budgets[str(args[1])] = args[2]  # type: ignore[assignment]
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {query}")

    async def fetchval(self, query: str, *args: object) -> Decimal | None:
        if "FROM budgets" in query:
            return self.budgets.get(str(args[0]))
        category = str(args[0])
        if "direction = $2" in query:
            direction, start, end = str(args[1]), args[2], args[3]
        else:
            start, end = args[1], args[2]
            direction = "expense" if "direction = 'expense'" in query else None
        return sum(
            (
                row["amount"]
                for row in self.transactions
                if row["category"] == category
                and row["ts"] >= start
                and row["ts"] < end
                and (direction is None or row["direction"] == direction)
            ),
            Decimal("0"),
        )

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        if "FROM budgets AS b" in query:
            start, end = args[0], args[1]
            return [
                {
                    "category": category,
                    "monthly_limit": limit,
                    "spent": sum(
                        (
                            row["amount"]
                            for row in self.transactions
                            if row["category"] == category
                            and row["direction"] == "expense"
                            and start <= row["ts"] < end
                        ),
                        Decimal("0"),
                    ),
                }
                for category, limit in sorted(self.budgets.items())
            ]

        start, end, category = args[:3]
        timezone = ZoneInfo(str(args[3])) if "GROUP BY day" in query else None
        filtered = [
            row
            for row in self.transactions
            if start <= row["ts"] < end and (category is None or row["category"] == category)
        ]
        grouped: dict[tuple[str, str], Decimal] = {}
        if "GROUP BY category" in query:
            for row in filtered:
                key = (row["category"], row["currency"])
                grouped[key] = grouped.get(key, Decimal("0")) + row["amount"]
            return [
                {"category": key[0], "currency": key[1], "total": value}
                for key, value in sorted(grouped.items())
            ]
        if timezone is not None:
            for row in filtered:
                key = (row["ts"].astimezone(timezone).date().isoformat(), row["currency"])
                grouped[key] = grouped.get(key, Decimal("0")) + row["amount"]
            return [
                {"day": datetime.fromisoformat(key[0]).date(), "currency": key[1], "total": value}
                for key, value in sorted(grouped.items())
            ]
        for row in filtered:
            key = ("", row["currency"])
            grouped[key] = grouped.get(key, Decimal("0")) + row["amount"]
        return [{"currency": key[1], "total": value} for key, value in sorted(grouped.items())]


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        update_id=101,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


async def test_add_transaction_quantizes_and_interprets_naive_time() -> None:
    pool = FakePool()
    result = await _add_transaction(
        pool,
        _context(),
        AddTransactionArgs(amount=1.005, direction="expense", ts="2026-07-10T12:00:00"),
    )

    row = pool.connection.transactions[0]
    assert result.status == "ok"
    assert row["amount"] == Decimal("1.01")
    assert row["ts"].utcoffset() == timedelta(hours=3)


async def test_add_transaction_rejects_non_positive_and_far_future_values() -> None:
    pool = FakePool()
    invalid = await _add_transaction(
        pool, _context(), AddTransactionArgs(amount=0, direction="expense")
    )
    future = await _add_transaction(
        pool,
        _context(),
        AddTransactionArgs(
            amount=1,
            direction="expense",
            ts=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
        ),
    )

    assert invalid.retryable is True
    assert future.status == "error"
    assert future.retryable is False
    assert pool.connection.transactions == []


async def test_add_transaction_today_total_filters_by_direction() -> None:
    pool = FakePool()
    income = await _add_transaction(
        pool, _context(), AddTransactionArgs(amount=100, direction="income", category="food")
    )
    expense = await _add_transaction(
        pool, _context(), AddTransactionArgs(amount=20, direction="expense", category="food")
    )
    later_income = await _add_transaction(
        pool, _context(), AddTransactionArgs(amount=15, direction="income", category="food")
    )

    assert income.payload["today_total"] == "100.00"
    assert expense.payload["today_total"] == "20.00"
    assert later_income.payload["today_total"] == "115.00"


async def test_query_validates_period_and_aggregates_category_day_and_none() -> None:
    pool = FakePool()
    local = ZoneInfo("Europe/Moscow")
    pool.connection.transactions = [
        {
            "amount": Decimal("10"),
            "direction": "expense",
            "category": "food",
            "currency": "RUB",
            "ts": datetime(2026, 7, 1, 10, tzinfo=local),
        },
        {
            "amount": Decimal("15"),
            "direction": "expense",
            "category": "food",
            "currency": "RUB",
            "ts": datetime(2026, 7, 2, 10, tzinfo=local),
        },
        {
            "amount": Decimal("20"),
            "direction": "expense",
            "category": "transport",
            "currency": "USD",
            "ts": datetime(2026, 7, 2, 11, tzinfo=local),
        },
    ]
    reversed_result = await _query_transactions(
        pool,
        None,
        _context(),
        QueryTransactionsArgs(period_start="2026-07-03", period_end="2026-07-01"),
    )
    long_result = await _query_transactions(
        pool,
        None,
        _context(),
        QueryTransactionsArgs(period_start="2025-01-01", period_end="2026-01-03"),
    )
    category = await _query_transactions(
        pool,
        None,
        _context(),
        QueryTransactionsArgs(period_start="2026-07-01", period_end="2026-07-02"),
    )
    day = await _query_transactions(
        pool,
        None,
        _context(),
        QueryTransactionsArgs(period_start="2026-07-01", period_end="2026-07-02", group_by="day"),
    )
    none = await _query_transactions(
        pool,
        None,
        _context(),
        QueryTransactionsArgs(period_start="2026-07-01", period_end="2026-07-02", group_by="none"),
    )

    assert reversed_result.status == long_result.status == "error"
    assert category.payload["rows"] == [
        {"total": "25.00", "category": "food", "currency": "RUB"},
        {"total": "20.00", "category": "transport", "currency": "USD"},
    ]
    assert [row["day"] for row in day.payload["rows"]] == ["2026-07-01", "2026-07-02", "2026-07-02"]
    assert none.payload["rows"] == [
        {"total": "25.00", "currency": "RUB"},
        {"total": "20.00", "currency": "USD"},
    ]


async def test_budget_alerts_fire_only_when_threshold_is_crossed() -> None:
    pool = FakePool()
    now = datetime.now(UTC)
    pool.connection.budgets["food"] = Decimal("100")
    pool.connection.transactions.append(
        {
            "amount": Decimal("70"),
            "direction": "expense",
            "category": "food",
            "currency": "RUB",
            "ts": now,
        }
    )
    eighty = await _add_transaction(
        pool, _context(), AddTransactionArgs(amount=15, direction="expense", category="food")
    )
    hundred = await _add_transaction(
        pool, _context(), AddTransactionArgs(amount=20, direction="expense", category="food")
    )
    already = await _add_transaction(
        pool, _context(), AddTransactionArgs(amount=1, direction="expense", category="food")
    )

    assert "80%" in str(eighty.payload["budget_alert"])
    assert "исчерпан" in str(hundred.payload["budget_alert"])
    assert "budget_alert" not in already.payload


async def test_budget_upsert_and_status() -> None:
    pool = FakePool()
    result = await _set_budget(
        pool, _context(), SetBudgetArgs(category="food", monthly_limit=200.005)
    )
    status = await _get_budget_status(pool, _context())

    assert result.payload["monthly_limit"] == "200.01"
    assert status.payload["budgets"] == [
        {"category": "food", "monthly_limit": "200.01", "spent": "0.00", "percent": 0.0}
    ]


async def test_long_daily_query_sends_chart_and_ignores_send_failure() -> None:
    pool = FakePool()
    pool.connection.transactions.append(
        {
            "amount": Decimal("10"),
            "direction": "expense",
            "category": "food",
            "currency": "RUB",
            "ts": datetime(2026, 7, 1, tzinfo=UTC),
        }
    )
    photos: list[bytes] = []

    async def send_photo(_chat_id: int, photo: bytes, _caption: str | None) -> None:
        photos.append(photo)
        raise RuntimeError("telegram unavailable")

    result = await _query_transactions(
        pool,
        send_photo,
        _context(),
        QueryTransactionsArgs(period_start="2026-07-01", period_end="2026-07-14", group_by="day"),
    )
    await asyncio.sleep(0)
    short = await _query_transactions(
        pool,
        send_photo,
        _context(),
        QueryTransactionsArgs(period_start="2026-07-01", period_end="2026-07-02", group_by="day"),
    )
    await asyncio.sleep(0)

    assert result.status == short.status == "ok"
    assert len(photos) == 1
    assert photos[0].startswith(b"\x89PNG")
