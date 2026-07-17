"""HTTP coverage for Mini App finance data and RLS boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from prometheus_client import CollectorRegistry

from webapp.app import create_app
from webapp.auth import create_session_token
from webapp.metrics import WebAppMetrics
from webapp.settings import WebAppSettings

SESSION_SECRET = "finance-test-session-secret"


class FinanceConnection:
    def __init__(
        self, users: dict[UUID, dict[str, str]], transactions: list[dict[str, Any]]
    ) -> None:
        self.users = users
        self.transactions = transactions
        self.current_user_id: UUID | None = None

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            self.current_user_id = UUID(str(args[0]))
            return "SELECT 1"
        if query.startswith("DELETE FROM transactions"):
            transaction_id = int(args[0])
            visible = next(
                (
                    item
                    for item in self.transactions
                    if item["id"] == transaction_id and item["user_id"] == self.current_user_id
                ),
                None,
            )
            if visible is None:
                return "DELETE 0"
            self.transactions.remove(visible)
            return "DELETE 1"
        raise AssertionError(query)

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        if "FROM users" in query:
            user = self.users.get(UUID(str(args[0])))
            return user.copy() if user and UUID(str(args[0])) == self.current_user_id else None
        if "transaction_count" in query:
            rows = self._range(args[0], args[1])
            return {
                "transaction_count": len(rows),
                "expense": sum(
                    (item["amount"] for item in rows if item["direction"] == "expense"),
                    Decimal(0),
                ),
            }
        raise AssertionError(query)

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        if "FROM budgets AS b" in query:
            return []
        if "SELECT id, ts" in query:
            start, end, category, cursor_ts, cursor_id, limit = args
            rows = [
                item
                for item in self._range(start, end)
                if category is None or item["category"] == category
            ]
            if cursor_ts is not None:
                rows = [item for item in rows if (item["ts"], item["id"]) < (cursor_ts, cursor_id)]
            rows.sort(key=lambda item: (item["ts"], item["id"]), reverse=True)
            return [item.copy() for item in rows[: int(limit)]]
        rows = self._range(args[0], args[1])
        if "GROUP BY direction" in query:
            return [
                {
                    "direction": direction,
                    "total": sum(
                        (item["amount"] for item in rows if item["direction"] == direction),
                        Decimal(0),
                    ),
                }
                for direction in ("expense", "income")
                if any(item["direction"] == direction for item in rows)
            ]
        if "GROUP BY category" in query:
            categories = sorted(
                {item["category"] for item in rows if item["direction"] == "expense"}
            )
            return [
                {
                    "category": category,
                    "expense": sum(
                        (
                            item["amount"]
                            for item in rows
                            if item["direction"] == "expense" and item["category"] == category
                        ),
                        Decimal(0),
                    ),
                }
                for category in categories
            ]
        if " AS bucket" in query:
            timezone = ZoneInfo(str(args[2]))
            grouped: dict[object, Decimal] = {}
            for item in rows:
                if item["direction"] != "expense":
                    continue
                bucket = item["ts"].astimezone(timezone).date()
                grouped[bucket] = grouped.get(bucket, Decimal(0)) + item["amount"]
            return [
                {"bucket": bucket, "expense": amount} for bucket, amount in sorted(grouped.items())
            ]
        raise AssertionError(query)

    def _range(self, start: object, end: object) -> list[dict[str, Any]]:
        return [
            item
            for item in self.transactions
            if item["user_id"] == self.current_user_id
            and item["currency"] == "RUB"
            and start <= item["ts"] < end
        ]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class FinancePool:
    def __init__(
        self, users: dict[UUID, dict[str, str]], transactions: list[dict[str, Any]]
    ) -> None:
        self.connection = FinanceConnection(users, transactions)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FinanceConnection]:
        yield self.connection


class AllowRedis:
    async def eval(self, _script: str, _numkeys: int, *_args: object) -> object:
        return [1, 0]

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _client(
    users: dict[UUID, dict[str, str]], transactions: list[dict[str, Any]]
) -> tuple[httpx.AsyncClient, WebAppMetrics]:
    metrics = WebAppMetrics(CollectorRegistry())
    app = create_app(
        settings=WebAppSettings(
            telegram_bot_token="finance-test-bot",
            session_secret=SESSION_SECRET,
            database_url="postgresql://unused",
            redis_cache_url="redis://unused",
        ),
        pool=FinancePool(users, transactions),  # type: ignore[arg-type]
        cache_redis=AllowRedis(),  # type: ignore[arg-type]
        metrics=metrics,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://finance.test"
    ), metrics


def _users(*user_ids: UUID) -> dict[UUID, dict[str, str]]:
    return {
        user_id: {"status": "active", "timezone": "Europe/Moscow", "plan": "trial"}
        for user_id in user_ids
    }


def _transaction(
    transaction_id: int, user_id: UUID, *, direction: str = "expense", category: str = "food"
) -> dict[str, Any]:
    return {
        "id": transaction_id,
        "user_id": user_id,
        "ts": datetime(2026, 7, 17, 9, transaction_id % 60, tzinfo=UTC),
        "amount": Decimal(str(transaction_id)),
        "direction": direction,
        "category": category,
        "currency": "RUB",
        "description": f"Transaction {transaction_id}",
    }


def _auth(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_session_token(user_id, SESSION_SECRET)}"}


async def test_finance_endpoints_require_auth_and_validate_transport() -> None:
    user_id = uuid4()
    client, _metrics = _client(_users(user_id), [])
    async with client:
        unauthenticated = await client.get("/app/api/finance/summary?from=2026-07-01&to=2026-07-31")
        reversed_range = await client.get(
            "/app/api/finance/summary?from=2026-07-31&to=2026-07-01", headers=_auth(user_id)
        )
        long_range = await client.get(
            "/app/api/finance/summary?from=2025-07-01&to=2026-07-02", headers=_auth(user_id)
        )
        category = await client.get(
            "/app/api/finance/transactions?from=2026-07-01&to=2026-07-31&category=investments",
            headers=_auth(user_id),
        )
        cursor = await client.get(
            "/app/api/finance/transactions?from=2026-07-01&to=2026-07-31&cursor=not-base64!",
            headers=_auth(user_id),
        )
        oversized = await client.get(
            f"/app/api/finance/transactions?from=2026-07-01&to=2026-07-31&cursor={'a' * 513}",
            headers=_auth(user_id),
        )

    assert unauthenticated.status_code == 401
    assert reversed_range.json()["error"]["code"] == "invalid_finance_range"
    assert long_range.status_code == 400
    assert category.status_code == 400
    assert cursor.json()["error"]["code"] == "invalid_cursor"
    assert oversized.status_code == 400


async def test_summary_response_separates_income_and_expense() -> None:
    user_id = uuid4()
    transactions = [
        _transaction(10, user_id),
        _transaction(100, user_id, direction="income", category="salary"),
    ]
    client, _metrics = _client(_users(user_id), transactions)
    async with client:
        response = await client.get(
            "/app/api/finance/summary?from=2026-07-01&to=2026-07-31",
            headers=_auth(user_id),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"] == {"expense": "10.00", "income": "100.00"}
    assert payload["by_category"] == [{"category": "food", "expense": "10.00", "share": 1.0}]
    assert payload["by_bucket"][0]["expense"] == "10.00"


async def test_pagination_has_no_duplicate_boundary_and_local_timestamps() -> None:
    user_id = uuid4()
    transactions = [_transaction(index, user_id) for index in range(1, 53)]
    client, _metrics = _client(_users(user_id), transactions)
    async with client:
        first = await client.get(
            "/app/api/finance/transactions?from=2026-07-01&to=2026-07-31",
            headers=_auth(user_id),
        )
        second = await client.get(
            "/app/api/finance/transactions",
            params={
                "from": "2026-07-01",
                "to": "2026-07-31",
                "cursor": first.json()["next_cursor"],
            },
            headers=_auth(user_id),
        )

    first_ids = {item["id"] for item in first.json()["items"]}
    second_ids = {item["id"] for item in second.json()["items"]}
    assert len(first_ids) == 50
    assert len(second_ids) == 2
    assert first_ids.isdisjoint(second_ids)
    assert first.json()["items"][0]["ts_local"].endswith("+03:00")


async def test_delete_is_rls_hidden_returns_204_and_increments_exact_metric() -> None:
    user_a, user_b = uuid4(), uuid4()
    transactions = [_transaction(1, user_a), _transaction(2, user_b)]
    client, metrics = _client(_users(user_a, user_b), transactions)
    async with client:
        foreign = await client.delete("/app/api/finance/transactions/2", headers=_auth(user_a))
        own = await client.delete("/app/api/finance/transactions/1", headers=_auth(user_a))
        missing = await client.delete("/app/api/finance/transactions/999", headers=_auth(user_a))

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == {
        "code": "transaction_not_found",
        "message": "Транзакция не найдена.",
        "trace_id": foreign.json()["error"]["trace_id"],
    }
    assert own.status_code == 204 and own.content == b""
    assert [item["id"] for item in transactions] == [2]
    assert metrics.transactions_deleted._value.get() == 1
