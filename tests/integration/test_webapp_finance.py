"""Live Postgres/Redis checks for Mini App finance RLS and deletion."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import asyncpg
import httpx
import pytest
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.queue import DEFAULT_REDIS_CACHE_URL
from core.rate_limit import webapp_rate_limit_key
from webapp.app import create_app
from webapp.auth import create_session_token
from webapp.metrics import WebAppMetrics
from webapp.settings import WebAppSettings

pytestmark = pytest.mark.integration
SESSION_SECRET = "finance-integration-session-secret"
MONTH_QUERY = {"from": "2030-07-01", "to": "2030-07-31"}


async def _cache_or_skip() -> Redis:
    client: Redis = Redis.from_url(
        os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL), decode_responses=True
    )
    try:
        await client.ping()
    except (OSError, RedisError) as exc:
        await client.aclose()
        pytest.skip(f"local dev redis is not reachable: {exc}")
    return client


async def test_finance_api_live_rls_pagination_filters_budgets_and_delete(
    postgres_roles: None,
    app_pool: asyncpg.Pool,
    migrator_connection: asyncpg.Connection,
) -> None:
    user_a, user_b = uuid4(), uuid4()
    telegram_a = -int(user_a.hex[:12], 16)
    telegram_b = -int(user_b.hex[:12], 16)
    cache = await _cache_or_skip()
    try:
        await migrator_connection.executemany(
            """
            INSERT INTO users (id, tg_user_id, tg_chat_id, timezone, plan, status)
            VALUES ($1, $2, $2, 'Europe/Moscow', 'trial', 'active')
            """,
            [(user_a, telegram_a), (user_b, telegram_b)],
        )
        food_rows = await migrator_connection.fetch(
            """
            INSERT INTO transactions (
                user_id, amount, direction, category, currency, description, ts, created_at
            )
            SELECT $1, 1.00, 'expense', 'food', 'RUB',
                   'food-' || series::text, $2, now()
            FROM generate_series(1, 52) AS series
            RETURNING id
            """,
            user_a,
            datetime(2030, 7, 17, 9, tzinfo=UTC),
        )
        own_expense = await migrator_connection.fetchval(
            """
            INSERT INTO transactions (
                user_id, amount, direction, category, currency, description, ts, created_at
            ) VALUES ($1, 125.25, 'expense', 'transport', 'RUB', 'taxi', $2, now())
            RETURNING id
            """,
            user_a,
            datetime(2030, 7, 17, 10, tzinfo=UTC),
        )
        own_income = await migrator_connection.fetchval(
            """
            INSERT INTO transactions (
                user_id, amount, direction, category, currency, description, ts, created_at
            ) VALUES ($1, 1000, 'income', 'salary', 'RUB', 'salary', $2, now())
            RETURNING id
            """,
            user_a,
            datetime(2030, 7, 17, 11, tzinfo=UTC),
        )
        foreign_expense = await migrator_connection.fetchval(
            """
            INSERT INTO transactions (
                user_id, amount, direction, category, currency, description, ts, created_at
            ) VALUES ($1, 999, 'expense', 'housing', 'RUB', 'foreign', $2, now())
            RETURNING id
            """,
            user_b,
            datetime(2030, 7, 17, 12, tzinfo=UTC),
        )
        await migrator_connection.execute(
            """
            INSERT INTO budgets (user_id, category, monthly_limit, created_at, updated_at)
            VALUES ($1, 'food', 500, now(), now())
            """,
            user_a,
        )

        app = create_app(
            settings=WebAppSettings(
                telegram_bot_token="finance-integration-bot",
                session_secret=SESSION_SECRET,
                database_url="postgresql://unused",
                redis_cache_url="redis://unused",
            ),
            pool=app_pool,
            cache_redis=cache,  # type: ignore[arg-type]
            metrics=WebAppMetrics(CollectorRegistry()),
        )
        headers_a = {"Authorization": f"Bearer {create_session_token(user_a, SESSION_SECRET)}"}
        headers_b = {"Authorization": f"Bearer {create_session_token(user_b, SESSION_SECRET)}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://finance.integration"
        ) as client:
            summary_before = await client.get(
                "/app/api/finance/summary", params=MONTH_QUERY, headers=headers_a
            )
            summary_b = await client.get(
                "/app/api/finance/summary", params=MONTH_QUERY, headers=headers_b
            )
            first_page = await client.get(
                "/app/api/finance/transactions", params=MONTH_QUERY, headers=headers_a
            )
            second_page = await client.get(
                "/app/api/finance/transactions",
                params={**MONTH_QUERY, "cursor": first_page.json()["next_cursor"]},
                headers=headers_a,
            )
            transport_filter = await client.get(
                "/app/api/finance/transactions",
                params={**MONTH_QUERY, "category": "transport"},
                headers=headers_a,
            )
            non_month = await client.get(
                "/app/api/finance/summary",
                params={"from": "2030-07-01", "to": "2030-07-30"},
                headers=headers_a,
            )
            foreign_delete = await client.delete(
                f"/app/api/finance/transactions/{foreign_expense}", headers=headers_a
            )
            own_delete = await client.delete(
                f"/app/api/finance/transactions/{own_expense}", headers=headers_a
            )
            summary_after = await client.get(
                "/app/api/finance/summary", params=MONTH_QUERY, headers=headers_a
            )
            list_after = await client.get(
                "/app/api/finance/transactions",
                params={**MONTH_QUERY, "category": "transport"},
                headers=headers_a,
            )

        assert summary_before.status_code == summary_b.status_code == 200
        before = summary_before.json()
        assert before["totals"] == {"expense": "177.25", "income": "1000.00"}
        assert [(item["category"], item["expense"]) for item in before["by_category"]] == [
            ("transport", "125.25"),
            ("food", "52.00"),
        ]
        assert before["by_category"][0]["share"] == pytest.approx(125.25 / 177.25)
        assert before["by_category"][1]["share"] == pytest.approx(52 / 177.25)
        assert summary_b.json()["totals"] == {"expense": "999.00", "income": "0.00"}
        assert before["budgets"] == [
            {"category": "food", "limit": "500.00", "spent": "52.00", "ratio": 0.104}
        ]
        assert non_month.status_code == 200
        assert non_month.json()["budgets"] == []

        first_items = first_page.json()["items"]
        second_items = second_page.json()["items"]
        food_ids = {int(row["id"]) for row in food_rows}
        expected_ids = food_ids | {int(own_expense), int(own_income)}
        first_ids = {item["id"] for item in first_items}
        second_ids = {item["id"] for item in second_items}
        assert first_page.status_code == second_page.status_code == 200
        assert transport_filter.status_code == 200
        assert len(first_items) == 50
        assert len(second_items) == 4
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == expected_ids
        assert first_page.json()["next_cursor"] is not None
        assert second_page.json()["next_cursor"] is None
        assert transport_filter.json()["items"] == [
            next(item for item in first_items if item["id"] == own_expense)
        ]

        assert foreign_delete.status_code == 404
        assert await migrator_connection.fetchval(
            "SELECT amount FROM transactions WHERE id = $1", foreign_expense
        ) == Decimal("999.00")
        assert own_delete.status_code == 204
        assert (
            await migrator_connection.fetchval(
                "SELECT amount FROM transactions WHERE id = $1", own_expense
            )
            is None
        )
        assert summary_after.status_code == list_after.status_code == 200
        assert summary_after.json()["totals"] == {"expense": "52.00", "income": "1000.00"}
        assert list_after.json() == {"items": [], "next_cursor": None}
    finally:
        try:
            await migrator_connection.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])", [user_a, user_b]
            )
        finally:
            await cache.delete(webapp_rate_limit_key(user_a), webapp_rate_limit_key(user_b))
            await cache.aclose()
