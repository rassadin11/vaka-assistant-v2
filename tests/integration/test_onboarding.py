"""Integration test for onboarding against Postgres and Redis."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator

import asyncpg
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.db import service_transaction
from core.envelope import UpdateEnvelope
from core.queue import DEFAULT_REDIS_CACHE_URL
from worker.onboarding import (
    START_TIMEZONE_PROMPT_TEXT,
    TIMEZONE_BUTTONS,
    WELCOME_CTA_TEXT,
    WELCOME_TEXT,
    OnboardingProcessor,
)
from worker.processor import EchoProcessor

pytestmark = pytest.mark.integration


async def _redis_or_skip(url: str) -> Redis:
    client: Redis = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except (OSError, RedisError) as exc:
        await client.aclose()
        pytest.skip(f"local dev redis is not reachable: {exc}")
    return client


@pytest.fixture
async def cache_redis() -> AsyncIterator[Redis]:
    client = await _redis_or_skip(os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL))
    try:
        yield client
    finally:
        await client.aclose()


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, list[list[tuple[str, str]]] | None]] = []
        self.callbacks: list[str] = []
        self.admin: list[str] = []

    async def send(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[tuple[str, str]]] | None = None,
    ) -> None:
        self.sent.append((chat_id, text, buttons))

    async def answer_callback(self, callback_query_id: str) -> None:
        self.callbacks.append(callback_query_id)

    async def notify_admin(self, text: str) -> None:
        self.admin.append(text)


def _envelope(
    update_id: int,
    tg_user_id: int,
    text: str,
    *,
    kind: str = "text",
    payload: dict[str, object] | None = None,
) -> UpdateEnvelope:
    return UpdateEnvelope.model_validate(
        {
            "update_id": update_id,
            "user_id": tg_user_id,
            "chat_id": tg_user_id,
            "kind": kind,
            "payload": {"text": text} if payload is None else payload,
        }
    )


async def test_onboarding_round_trip_against_postgres(
    service_pool: asyncpg.Pool,
    cache_redis: Redis,
) -> None:
    update_id = int(time.time() * 1000) % 1_000_000_000
    tg_user_id = 990_100_000 + update_id
    admin_id = 990_200_000 + update_id
    recorder = Recorder()
    processor = OnboardingProcessor(
        service_pool=service_pool,
        cache_redis=cache_redis,
        inner=EchoProcessor(),
        send=recorder.send,
        answer_callback=recorder.answer_callback,
        notify_admin=recorder.notify_admin,
        admin_ids=(admin_id,),
    )

    try:
        await cache_redis.delete(
            f"onboarding:tz_pending:{tg_user_id}",
            f"onboarding:rejected_notified:{tg_user_id}",
        )
        async with service_transaction(service_pool) as connection:
            await connection.execute("DELETE FROM users WHERE tg_user_id = $1", tg_user_id)

        first_reply = await processor.process(_envelope(update_id, tg_user_id, "/start"))
        timezone_reply = await processor.process(
            _envelope(
                update_id + 1,
                tg_user_id,
                "",
                kind="callback",
                payload={
                    "data": "tz:Europe/Kaliningrad",
                    "message_id": 10,
                    "callback_query_id": "cb-integration",
                },
            )
        )
        echo_reply = await processor.process(_envelope(update_id + 2, tg_user_id, "echo me"))

        async with service_transaction(service_pool) as connection:
            row = await connection.fetchrow(
                "SELECT status, timezone FROM users WHERE tg_user_id = $1",
                tg_user_id,
            )
    finally:
        await cache_redis.delete(
            f"onboarding:tz_pending:{tg_user_id}",
            f"onboarding:rejected_notified:{tg_user_id}",
        )
        async with service_transaction(service_pool) as connection:
            await connection.execute("DELETE FROM users WHERE tg_user_id = $1", tg_user_id)

    assert first_reply is None
    assert recorder.sent == [
        (tg_user_id, START_TIMEZONE_PROMPT_TEXT, TIMEZONE_BUTTONS),
        (tg_user_id, WELCOME_TEXT.format(tz="Europe/Kaliningrad"), None),
        (tg_user_id, WELCOME_CTA_TEXT, None),
    ]
    assert recorder.callbacks == ["cb-integration"]
    assert timezone_reply is None
    assert echo_reply == "echo me"
    assert row is not None
    assert row["status"] == "active"
    assert row["timezone"] == "Europe/Kaliningrad"
