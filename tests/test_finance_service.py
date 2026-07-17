"""Pure and SQL-boundary tests for the shared finance service."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from core.finance_service import (
    InvalidFinanceCursor,
    InvalidFinanceRange,
    bucket_granularity,
    decode_cursor,
    encode_cursor,
    fetch_summary,
    fetch_transactions_page,
    inclusive_date_bounds,
    previous_same_length_bounds,
    quantize_money,
)


class SummaryConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        self.queries.append(query)
        if "GROUP BY direction" in query:
            return [
                {"direction": "expense", "total": Decimal("30.005")},
                {"direction": "income", "total": Decimal("100")},
            ]
        if "GROUP BY category" in query and "FROM budgets" not in query:
            return [
                {"category": "food", "expense": Decimal("20")},
                {"category": "transport", "expense": Decimal("10.005")},
            ]
        if " AS bucket" in query:
            assert "direction = 'expense'" in query
            return [{"bucket": date(2026, 7, 1), "expense": Decimal("30.005")}]
        if "FROM budgets AS b" in query:
            return [
                {
                    "category": "food",
                    "monthly_limit": Decimal("100"),
                    "spent": Decimal("20"),
                }
            ]
        raise AssertionError(query)

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any]:
        self.queries.append(query)
        return {"transaction_count": 1, "expense": Decimal("12.345")}

    async def execute(self, query: str, *args: object) -> str:
        raise AssertionError(query)


class PageConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.arguments: tuple[object, ...] = ()

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        assert "(ts, id) <" in query
        assert "ORDER BY ts DESC, id DESC" in query
        self.arguments = args
        return self.rows

    async def fetchrow(self, query: str, *args: object) -> None:
        raise AssertionError(query)

    async def execute(self, query: str, *args: object) -> str:
        raise AssertionError(query)


def test_money_quantization_is_half_up_and_rejects_invalid_values() -> None:
    assert quantize_money("1.005") == Decimal("1.01")
    assert quantize_money("1.004") == Decimal("1.00")
    assert quantize_money("-2.675") == Decimal("-2.68")
    assert quantize_money("invalid") is None
    assert quantize_money("NaN") is None
    assert quantize_money("Infinity") is None


def test_local_range_and_previous_period_preserve_dst_midnights() -> None:
    timezone = ZoneInfo("Europe/Berlin")
    start, end, days = inclusive_date_bounds(date(2026, 3, 28), date(2026, 3, 30), timezone)
    previous_start, previous_end = previous_same_length_bounds(
        date(2026, 3, 28), date(2026, 3, 30), timezone
    )

    assert days == 3
    assert end - start == timedelta(hours=71)
    assert previous_end == start
    assert previous_end - previous_start == timedelta(days=3)


def test_bucket_granularity_boundaries_and_rejection() -> None:
    assert bucket_granularity(1) == bucket_granularity(31) == "day"
    assert bucket_granularity(32) == bucket_granularity(180) == "week"
    assert bucket_granularity(181) == bucket_granularity(366) == "month"
    with pytest.raises(InvalidFinanceRange):
        bucket_granularity(367)


async def test_summary_separates_directions_quantizes_and_only_loads_monthly_budgets() -> None:
    connection = SummaryConnection()
    result = await fetch_summary(connection, "Europe/Moscow", date(2026, 7, 1), date(2026, 7, 31))

    assert result.totals.expense == Decimal("30.01")
    assert result.totals.income == Decimal("100.00")
    assert result.previous_period is not None
    assert result.previous_period.expense == Decimal("12.35")
    assert [item.expense for item in result.by_category] == [
        Decimal("20.00"),
        Decimal("10.01"),
    ]
    assert result.budgets[0].ratio == 0.2
    assert all(
        "currency = 'RUB'" in query for query in connection.queries if "transactions" in query
    )

    connection = SummaryConnection()
    result = await fetch_summary(connection, "Europe/Moscow", date(2026, 7, 2), date(2026, 7, 31))
    assert result.budgets == ()
    assert not any("FROM budgets AS b" in query for query in connection.queries)


async def test_page_uses_limit_plus_one_local_time_and_keyset_cursor() -> None:
    rows = [
        {
            "id": index,
            "ts": datetime(2026, 7, 20, 12, tzinfo=UTC) - timedelta(minutes=index),
            "amount": Decimal("5"),
            "direction": "expense",
            "category": "food",
            "description": f"row {index}",
        }
        for index in range(51, 0, -1)
    ]
    connection = PageConnection(rows)
    page = await fetch_transactions_page(
        connection,
        "Europe/Moscow",
        date(2026, 7, 1),
        date(2026, 7, 31),
        category="food",
    )

    assert len(page.items) == 50
    assert page.items[0].ts_local.utcoffset() == timedelta(hours=3)
    assert page.next_cursor is not None
    cursor_timestamp, cursor_id = decode_cursor(page.next_cursor)
    assert (cursor_timestamp, cursor_id) == (rows[49]["ts"], rows[49]["id"])
    assert connection.arguments[-1] == 51


def test_cursor_is_versioned_strict_and_size_bounded() -> None:
    timestamp = datetime(2026, 7, 17, 12, tzinfo=UTC)
    assert decode_cursor(encode_cursor(timestamp, 9)) == (timestamp, 9)

    malformed = ["!", "a" * 513]
    for payload in (
        {"v": 2, "ts": "2026-07-17T12:00:00Z", "id": 9},
        {"v": 1, "ts": "2026-07-17T12:00:00+03:00", "id": 9},
        {"v": 1, "ts": "2026-07-17T12:00:00Z", "id": True},
        {"v": 1, "ts": "2026-07-17T12:00:00Z", "id": 9, "user_id": "x"},
    ):
        malformed.append(
            base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        )
    for cursor in malformed:
        with pytest.raises(InvalidFinanceCursor):
            decode_cursor(cursor)
