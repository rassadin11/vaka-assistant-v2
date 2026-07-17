"""Integration checks for the Mini App SECURITY DEFINER user resolver."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.queue import DEFAULT_REDIS_CACHE_URL
from core.rate_limit import webapp_rate_limit_key
from webapp.app import create_app
from webapp.metrics import WebAppMetrics
from webapp.settings import WebAppSettings

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
MIGRATIONS_DATABASE_URL = "postgresql+psycopg://migrator:dev-local-only@127.0.0.1:5432/assistant"
TEST_BOT_TOKEN = "integration-test-bot-token"
TEST_SESSION_SECRET = "integration-test-session-secret"
TEST_NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


def _run_alembic_upgrade() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")


async def _cache_redis_or_skip() -> Redis:
    client: Redis = Redis.from_url(
        os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL), decode_responses=True
    )
    try:
        await client.ping()
    except (OSError, RedisError) as exc:
        await client.aclose()
        pytest.skip(f"local dev redis is not reachable: {exc}")
    return client


def _init_data(telegram_user_id: int) -> str:
    pairs = [
        ("auth_date", str(int(TEST_NOW.timestamp()))),
        ("user", json.dumps({"id": telegram_user_id})),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs.append(("hash", hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()))
    return urlencode(pairs)


def _webapp_settings() -> WebAppSettings:
    return WebAppSettings(
        telegram_bot_token=TEST_BOT_TOKEN,
        session_secret=TEST_SESSION_SECRET,
        database_url="postgresql://unused",
        redis_cache_url="redis://unused",
    )


async def test_webapp_resolver_is_app_only_and_preserves_fail_closed_rls(
    monkeypatch: pytest.MonkeyPatch,
    postgres_roles: None,
    app_pool: asyncpg.Pool,
    migrator_connection: asyncpg.Connection,
) -> None:
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", MIGRATIONS_DATABASE_URL)
    await asyncio.to_thread(_run_alembic_upgrade)

    user_id = uuid4()
    telegram_user_id = -int(user_id.hex[:12], 16)
    try:
        await migrator_connection.execute(
            """
            INSERT INTO users (id, tg_user_id, tg_chat_id, timezone, status)
            VALUES ($1, $2, $2, 'Europe/Moscow', 'active')
            """,
            user_id,
            telegram_user_id,
        )

        public_can_execute = await migrator_connection.fetchval(
            "SELECT has_function_privilege("
            "'public', 'public.webapp_resolve_user(bigint)', 'EXECUTE'"
            ")"
        )
        async with app_pool.acquire() as connection:
            resolved = await connection.fetchrow(
                "SELECT user_id, status FROM public.webapp_resolve_user($1)", telegram_user_id
            )
            unknown = await connection.fetchrow(
                "SELECT user_id, status FROM public.webapp_resolve_user($1)", telegram_user_id + 1
            )
            visible_without_rls_context = await connection.fetchval("SELECT count(*) FROM users")
    finally:
        await migrator_connection.execute("DELETE FROM users WHERE id = $1", user_id)

    assert public_can_execute is False
    assert resolved is not None
    assert set(resolved.keys()) == {"user_id", "status"}
    assert resolved["user_id"] == user_id
    assert resolved["status"] == "active"
    assert unknown is None
    assert visible_without_rls_context == 0


async def test_webapp_http_auth_and_me_are_rls_isolated_and_recheck_status(
    monkeypatch: pytest.MonkeyPatch,
    postgres_roles: None,
    app_pool: asyncpg.Pool,
    migrator_connection: asyncpg.Connection,
) -> None:
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", MIGRATIONS_DATABASE_URL)
    await asyncio.to_thread(_run_alembic_upgrade)
    cache_redis = await _cache_redis_or_skip()

    user_a = uuid4()
    user_b = uuid4()
    telegram_user_a = -int(user_a.hex[:12], 16)
    telegram_user_b = -int(user_b.hex[:12], 16)
    try:
        await migrator_connection.executemany(
            """
            INSERT INTO users (id, tg_user_id, tg_chat_id, timezone, plan, status)
            VALUES ($1, $2, $2, $3, $4, 'active')
            """,
            [
                (user_a, telegram_user_a, "Europe/Moscow", "trial"),
                (user_b, telegram_user_b, "Asia/Tokyo", "basic"),
            ],
        )
        app = create_app(
            settings=_webapp_settings(),
            pool=app_pool,
            cache_redis=cache_redis,  # type: ignore[arg-type]
            clock=lambda: TEST_NOW,
            metrics=WebAppMetrics(CollectorRegistry()),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://webapp.integration"
        ) as client:
            auth_a = await client.post(
                "/app/api/auth", json={"init_data": _init_data(telegram_user_a)}
            )
            auth_b = await client.post(
                "/app/api/auth", json={"init_data": _init_data(telegram_user_b)}
            )
            assert auth_a.status_code == 200
            assert auth_b.status_code == 200
            token_a = auth_a.json()["token"]
            token_b = auth_b.json()["token"]

            me_a = await client.get("/app/api/me", headers={"Authorization": f"Bearer {token_a}"})
            me_b = await client.get("/app/api/me", headers={"Authorization": f"Bearer {token_b}"})
            await migrator_connection.execute(
                "UPDATE users SET status = 'banned' WHERE id = $1", user_a
            )
            banned_a = await client.get(
                "/app/api/me", headers={"Authorization": f"Bearer {token_a}"}
            )
    finally:
        await migrator_connection.execute(
            "DELETE FROM users WHERE id = ANY($1::uuid[])", [user_a, user_b]
        )
        await cache_redis.delete(webapp_rate_limit_key(user_a), webapp_rate_limit_key(user_b))
        await cache_redis.aclose()

    assert me_a.status_code == 200
    assert me_a.json() == {"timezone": "Europe/Moscow", "plan": "trial"}
    assert me_b.status_code == 200
    assert me_b.json() == {"timezone": "Asia/Tokyo", "plan": "basic"}
    for response in (me_a, me_b):
        body = response.text
        assert "id" not in response.json()
        assert "tg_user_id" not in body
        assert str(user_a) not in body
        assert str(user_b) not in body
    assert banned_a.status_code == 403
