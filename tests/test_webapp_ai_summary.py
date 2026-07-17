"""HTTP coverage for the current Mini App finance AI-summary endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from prometheus_client import CollectorRegistry

from core.llm import LLMProviderError
from core.llm_mock import MockLLMProvider, mock_text_response
from webapp.app import create_app
from webapp.auth import create_session_token
from webapp.metrics import WebAppMetrics
from webapp.settings import WebAppSettings

SESSION_SECRET = "finance-ai-test-session-secret"


class FinanceConnection:
    def __init__(
        self, users: dict[UUID, dict[str, str]], transactions: list[dict[str, Any]]
    ) -> None:
        self.users = users
        self.transactions = transactions
        self.current_user_id: UUID | None = None
        self.usage_rows = 0

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
        if "INSERT INTO usage" in query:
            self.usage_rows += 1
            return "INSERT 0 1"
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
        rows = self._range(args[0], args[1])
        if "FROM budgets AS b" in query:
            return []
        if "ORDER BY amount DESC" in query:
            expenses = [item for item in rows if item["direction"] == "expense"]
            expenses.sort(key=lambda item: (item["amount"], item["id"]), reverse=True)
            return [item.copy() for item in expenses[: int(args[2])]]
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
            return [{"bucket": rows[0]["ts"].date(), "expense": rows[0]["amount"]}] if rows else []
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


class MemoryRedis:
    def __init__(self, *, allow: bool = True, spent: str | None = None) -> None:
        self.allow = allow
        self.spent = spent
        self.values: dict[str, str] = {}

    async def eval(self, _script: str, _numkeys: int, *_args: object) -> object:
        return [1 if self.allow else 0, 0]

    async def get(self, name: str) -> str | None:
        if name.startswith("spend_rub:"):
            return self.spent
        return self.values.get(name)

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        _ = ex
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    async def delete(self, *names: str) -> None:
        for name in names:
            self.values.pop(name, None)

    async def incr(self, name: str) -> int:
        next_value = int(self.values.get(name, "0")) + 1
        self.values[name] = str(next_value)
        return next_value

    async def incrbyfloat(self, name: str, amount: float) -> float:
        next_value = float(self.values.get(name, "0")) + amount
        self.values[name] = str(next_value)
        return next_value

    async def expire(self, _name: str, _seconds: int) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


async def test_ai_summary_statuses_and_auth_rate_limit() -> None:
    user_id = uuid4()
    cases = [
        ([], MockLLMProvider.scripted([mock_text_response("unused")]), None, "empty"),
        ([_transaction(10, user_id)], None, None, "unavailable"),
        (
            [_transaction(10, user_id)],
            MockLLMProvider.scripted([LLMProviderError("down")]),
            None,
            "unavailable",
        ),
        (
            [_transaction(10, user_id)],
            MockLLMProvider.scripted([mock_text_response("Сводка.")]),
            "15",
            "budget_exhausted",
        ),
        (
            [_transaction(10, user_id)],
            MockLLMProvider.scripted([mock_text_response("Сводка.")]),
            None,
            "ready",
        ),
    ]
    for transactions, provider, spent, expected in cases:
        client, _cache, _pool = _client(
            _users(user_id), transactions, provider=provider, queue=MemoryRedis(spent=spent)
        )
        async with client:
            response = await client.get(
                "/app/api/finance/ai-summary?from=2026-07-01&to=2026-07-31",
                headers=_auth(user_id),
            )
        assert response.status_code == 200
        assert response.json()["status"] == expected

    client, _cache, _pool = _client(_users(user_id), [], provider=None)
    limited, _cache_limited, _pool_limited = _client(
        _users(user_id), [], provider=None, cache=MemoryRedis(allow=False)
    )
    async with client, limited:
        no_bearer = await client.get("/app/api/finance/ai-summary?from=2026-07-01&to=2026-07-31")
        rate_limited = await limited.get(
            "/app/api/finance/ai-summary?from=2026-07-01&to=2026-07-31",
            headers=_auth(user_id),
        )
    assert no_bearer.status_code == 401
    assert rate_limited.status_code == 429


async def test_delete_bumps_finance_summary_generation() -> None:
    user_id = uuid4()
    cache = MemoryRedis()
    client, cache, _pool = _client(
        _users(user_id), [_transaction(10, user_id)], provider=None, cache=cache
    )
    async with client:
        response = await client.delete("/app/api/finance/transactions/10", headers=_auth(user_id))

    assert response.status_code == 204
    assert cache.values[f"fin:gen:{user_id}"] == "1"


def _client(
    users: dict[UUID, dict[str, str]],
    transactions: list[dict[str, Any]],
    *,
    provider: MockLLMProvider | None,
    cache: MemoryRedis | None = None,
    queue: MemoryRedis | None = None,
) -> tuple[httpx.AsyncClient, MemoryRedis, FinancePool]:
    metrics = WebAppMetrics(CollectorRegistry())
    cache = cache or MemoryRedis()
    queue = queue or MemoryRedis()
    pool = FinancePool(users, transactions)
    app = create_app(
        settings=WebAppSettings(
            telegram_bot_token="finance-ai-test-bot",
            session_secret=SESSION_SECRET,
            database_url="postgresql://unused",
            redis_cache_url="redis://unused",
        ),
        pool=pool,  # type: ignore[arg-type]
        cache_redis=cache,  # type: ignore[arg-type]
        queue_redis=queue,  # type: ignore[arg-type]
        llm_provider=provider,
        metrics=metrics,
        clock=lambda: datetime(2026, 7, 17, tzinfo=UTC),
    )
    return (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://finance-ai.test"
        ),
        cache,
        pool,
    )


def _users(*user_ids: UUID) -> dict[UUID, dict[str, str]]:
    return {
        user_id: {"status": "active", "timezone": "UTC", "plan": "trial"} for user_id in user_ids
    }


def _transaction(transaction_id: int, user_id: UUID) -> dict[str, Any]:
    return {
        "id": transaction_id,
        "user_id": user_id,
        "ts": datetime(2026, 7, 17, 9, transaction_id % 60, tzinfo=UTC),
        "amount": Decimal(str(transaction_id)),
        "direction": "expense",
        "category": "food",
        "currency": "RUB",
        "description": f"Transaction {transaction_id}",
    }


def _auth(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_session_token(user_id, SESSION_SECRET)}"}
