"""HTTP-level tests for the Mini App platform foundation."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from prometheus_client import CollectorRegistry
from pytest import MonkeyPatch

from webapp.app import create_app
from webapp.auth import create_session_token
from webapp.metrics import WebAppMetrics
from webapp.settings import WebAppSettings

BOT_TOKEN = "test-bot-token"
SESSION_SECRET = "test-session-secret"
NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


class FakeConnection:
    def __init__(self, users: dict[int, dict[str, object]]) -> None:
        self.users = users
        self.current_user_id: UUID | None = None

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "webapp_resolve_user" in query:
            telegram_user_id = args[0]
            assert isinstance(telegram_user_id, int)
            user = self.users.get(telegram_user_id)
            if user is None:
                return None
            return {"user_id": user["id"], "status": user["status"]}
        assert self.current_user_id == args[0]
        for user in self.users.values():
            if user["id"] == self.current_user_id:
                return {
                    "status": user["status"],
                    "timezone": user["timezone"],
                    "plan": user["plan"],
                }
        return None

    async def fetchval(self, query: str) -> object:
        assert query == "SELECT 1"
        return 1

    async def execute(self, query: str, *args: object) -> str:
        if "set_config" in query:
            self.current_user_id = UUID(str(args[0]))
        return "SELECT 1"

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class FakePool:
    def __init__(self, users: dict[int, dict[str, object]]) -> None:
        self.connection = FakeConnection(users)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConnection]:
        yield self.connection


class FakeRedis:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.keys: list[str] = []

    async def eval(self, _script: str, _numkeys: int, *keys_and_args: object) -> object:
        self.keys.append(str(keys_and_args[0]))
        return [int(self.allowed), 0]

    async def ping(self) -> object:
        return True

    async def aclose(self) -> None:
        return None


def _settings() -> WebAppSettings:
    return WebAppSettings(
        telegram_bot_token=BOT_TOKEN,
        session_secret=SESSION_SECRET,
        database_url="postgresql://unused",
        redis_cache_url="redis://unused",
    )


def _init_data(telegram_user_id: int) -> str:
    pairs = [
        ("auth_date", str(int(NOW.timestamp()))),
        ("user", json.dumps({"id": telegram_user_id})),
    ]
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs.append(("hash", hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()))
    return urlencode(pairs)


def _client(
    users: dict[int, dict[str, object]], redis: FakeRedis | None = None
) -> httpx.AsyncClient:
    app = create_app(
        settings=_settings(),
        pool=FakePool(users),  # type: ignore[arg-type]
        cache_redis=redis if redis is not None else FakeRedis(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        metrics=WebAppMetrics(CollectorRegistry()),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://webapp.test")


async def test_auth_and_me_only_expose_timezone_and_plan() -> None:
    user_id = uuid4()
    users = {42: {"id": user_id, "status": "active", "timezone": "Europe/Moscow", "plan": "trial"}}
    async with _client(users) as client:
        auth_response = await client.post("/app/api/auth", json={"init_data": _init_data(42)})
        assert auth_response.status_code == 200
        token = auth_response.json()["token"]
        me_response = await client.get("/app/api/me", headers={"Authorization": f"Bearer {token}"})

    assert me_response.status_code == 200
    assert me_response.json() == {"timezone": "Europe/Moscow", "plan": "trial"}


async def test_successful_auth_increments_app_opened_metric() -> None:
    registry = CollectorRegistry()
    metrics = WebAppMetrics(registry)
    user_id = uuid4()
    users = {
        42: {
            "id": user_id,
            "status": "active",
            "timezone": "Europe/Moscow",
            "plan": "trial",
        }
    }
    app = create_app(
        settings=_settings(),
        pool=FakePool(users),  # type: ignore[arg-type]
        cache_redis=FakeRedis(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        metrics=metrics,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://webapp.test"
    ) as client:
        response = await client.post("/app/api/auth", json={"init_data": _init_data(42)})

    assert response.status_code == 200
    assert registry.get_sample_value("webapp_app_opened_total") == 1


async def test_auth_rejects_unknown_and_non_active_users() -> None:
    users = {42: {"id": uuid4(), "status": "pending", "timezone": "UTC", "plan": "trial"}}
    async with _client(users) as client:
        non_active = await client.post("/app/api/auth", json={"init_data": _init_data(42)})
        unknown = await client.post("/app/api/auth", json={"init_data": _init_data(999)})

    assert non_active.status_code == 403
    assert unknown.status_code == 403


async def test_me_rechecks_current_status_and_uses_a_separate_webapp_rate_limit_key() -> None:
    user_id = uuid4()
    redis = FakeRedis()
    users = {42: {"id": user_id, "status": "banned", "timezone": "UTC", "plan": "trial"}}
    token = create_session_token(user_id, SESSION_SECRET)
    async with _client(users, redis) as client:
        response = await client.get("/app/api/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert redis.keys == [f"rl:webapp:{user_id}"]


async def test_me_rejects_missing_or_invalid_bearer_and_rate_limit_with_error_envelopes() -> None:
    user_id = uuid4()
    users = {42: {"id": user_id, "status": "active", "timezone": "UTC", "plan": "trial"}}
    async with _client(users) as client:
        missing = await client.get("/app/api/me")
        invalid = await client.get("/app/api/me", headers={"Authorization": "Bearer malformed"})
    async with _client(users, FakeRedis(allowed=False)) as client:
        token = create_session_token(user_id, SESSION_SECRET)
        limited = await client.get(
            "/app/api/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    for response, expected_status in ((missing, 401), (invalid, 401), (limited, 429)):
        assert response.status_code == expected_status
        assert set(response.json()["error"]) == {"code", "message", "trace_id"}
        assert response.headers["X-Trace-Id"] == response.json()["error"]["trace_id"]


async def test_invalid_init_data_unknown_api_route_and_health_are_json_or_healthy() -> None:
    users = {42: {"id": uuid4(), "status": "active", "timezone": "UTC", "plan": "trial"}}
    async with _client(users) as client:
        invalid = await client.post("/app/api/auth", json={"init_data": "not-valid"})
        missing_route = await client.get("/app/api/unknown")
        health = await client.get("/app/healthz")

    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "invalid_init_data"
    assert missing_route.status_code == 404
    assert missing_route.json()["error"]["code"] == "not_found"
    assert health.status_code == 200


async def test_static_spa_fallback_and_hashed_asset_cache_policy(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    static_directory = tmp_path / "static"
    assets_directory = static_directory / "assets"
    assets_directory.mkdir(parents=True)
    (static_directory / "index.html").write_text("<main>Mini App</main>", encoding="utf-8")
    (assets_directory / "app-abc123.js").write_text("console.log('ok')", encoding="utf-8")
    monkeypatch.setattr("webapp.app.STATIC_DIRECTORY", static_directory)

    async with _client({}) as client:
        shell = await client.get("/app")
        deep_link = await client.get("/app/settings")
        asset = await client.get("/app/assets/app-abc123.js")
        api = await client.get("/app/api/not-a-static-route")

    assert shell.text == "<main>Mini App</main>"
    assert shell.headers["cache-control"] == "no-cache"
    assert deep_link.text == shell.text
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert api.status_code == 404
    assert api.json()["error"]["code"] == "not_found"


async def test_auth_rejects_extra_request_fields() -> None:
    users = {42: {"id": uuid4(), "status": "active", "timezone": "UTC", "plan": "trial"}}
    async with _client(users) as client:
        response = await client.post(
            "/app/api/auth",
            json={"init_data": _init_data(42), "unexpected": "forbidden"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
