"""Live Postgres/Redis checks for Mini App calendar RLS and mutations."""

from __future__ import annotations

import os
from datetime import UTC, datetime
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
SESSION_SECRET = "calendar-integration-session-secret"


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


async def test_calendar_api_uses_live_rls_and_shared_scheduled_tasks(
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
        foreign_id = await migrator_connection.fetchval(
            """
            INSERT INTO scheduled_tasks (
                user_id, kind, title, payload, next_run_at, status, created_at
            ) VALUES ($1, 'reminder', 'foreign', 'foreign', $2, 'active', now())
            RETURNING id
            """,
            user_b,
            datetime(2030, 7, 18, 7, tzinfo=UTC),
        )
        done_id = await migrator_connection.fetchval(
            """
            INSERT INTO scheduled_tasks (
                user_id, kind, title, payload, next_run_at, status, created_at
            ) VALUES ($1, 'agent_task', 'Готово', 'done', $2, 'done', now())
            RETURNING id
            """,
            user_a,
            datetime(2030, 7, 18, 8, tzinfo=UTC),
        )
        await migrator_connection.execute(
            """
            INSERT INTO scheduled_tasks (
                user_id, kind, title, payload, next_run_at, status, created_at
            ) VALUES ($1, 'reminder', 'cancelled', 'cancelled', $2, 'cancelled', now())
            """,
            user_a,
            datetime(2030, 7, 18, 9, tzinfo=UTC),
        )
        app = create_app(
            settings=WebAppSettings(
                telegram_bot_token="calendar-integration-bot",
                session_secret=SESSION_SECRET,
                database_url="postgresql://unused",
                redis_cache_url="redis://unused",
            ),
            pool=app_pool,
            cache_redis=cache,  # type: ignore[arg-type]
            clock=lambda: datetime(2030, 7, 17, 9, tzinfo=UTC),
            metrics=WebAppMetrics(CollectorRegistry()),
        )
        token = create_session_token(user_a, SESSION_SECRET)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://calendar.integration"
        ) as client:
            created = await client.post(
                "/app/api/reminders",
                headers=headers,
                json={"text": "Из Mini App", "remind_at_local": "2030-07-18T10:00"},
            )
            calendar = await client.get(
                "/app/api/calendar?from=2030-07-18&to=2030-07-18", headers=headers
            )
            foreign = await client.delete(f"/app/api/scheduled/{foreign_id}", headers=headers)
            cancelled = await client.delete(
                f"/app/api/scheduled/{created.json()['id']}", headers=headers
            )
            after = await client.get(
                "/app/api/calendar?from=2030-07-18&to=2030-07-18", headers=headers
            )
            done_conflict = await client.delete(f"/app/api/scheduled/{done_id}", headers=headers)

        before_ids = [item["id"] for item in calendar.json()["days"]["2030-07-18"]]
        after_ids = [item["id"] for item in after.json()["days"]["2030-07-18"]]
        assert created.status_code == 201
        assert created.json()["id"] in before_ids
        assert done_id in before_ids
        assert foreign.status_code == 404
        assert cancelled.status_code == 200
        assert created.json()["id"] not in after_ids
        assert after_ids == [done_id]
        assert done_conflict.status_code == 409
    finally:
        await migrator_connection.execute(
            "DELETE FROM users WHERE id = ANY($1::uuid[])", [user_a, user_b]
        )
        await cache.delete(webapp_rate_limit_key(user_a), webapp_rate_limit_key(user_b))
        await cache.aclose()
