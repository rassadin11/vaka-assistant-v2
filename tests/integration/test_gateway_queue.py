"""Integration tests: gateway webhook against the local Redis compose stack."""

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.envelope import UpdateEnvelope
from core.queue import (
    DEFAULT_REDIS_CACHE_URL,
    DEFAULT_REDIS_QUEUE_URL,
    partition_for_user,
    stream_key,
)
from gateway.app import create_app
from gateway.config import GatewayConfig
from tests.test_gateway import SECRET_PATH, SECRET_TOKEN, _config, _text_update

pytestmark = pytest.mark.integration

# High ids keep test traffic apart from anything a developer produced manually.
TEST_USER_ID = 990_000_101
TEST_UPDATE_ID = 880_000_101


async def _redis_or_skip(url: str) -> Redis:
    client: Redis = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except (OSError, RedisError) as exc:
        await client.aclose()
        pytest.skip(f"local dev redis is not reachable: {exc}")
    return client


@pytest.fixture
async def queue_redis() -> AsyncIterator[Redis]:
    client = await _redis_or_skip(os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def cache_redis() -> AsyncIterator[Redis]:
    client = await _redis_or_skip(os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL))
    try:
        yield client
    finally:
        await client.aclose()


def _update(update_id: int = TEST_UPDATE_ID) -> dict[str, Any]:
    update = _text_update(update_id=update_id)
    update["message"]["from"]["id"] = TEST_USER_ID
    update["message"]["chat"]["id"] = TEST_USER_ID
    return update


async def _cleanup(queue_redis: Redis, cache_redis: Redis, config: GatewayConfig) -> None:
    key = stream_key("interactive", partition_for_user(TEST_USER_ID))
    entries = await queue_redis.xrange(key, "-", "+")
    for entry_id, fields in entries:
        envelope = UpdateEnvelope.from_stream_entry(fields)
        if envelope.user_id == TEST_USER_ID:
            await queue_redis.xdel(key, entry_id)
    await cache_redis.delete(f"dedup:{TEST_UPDATE_ID}")
    del config  # unused, kept for signature symmetry


async def test_webhook_enqueues_to_expected_partition_and_dedups(
    queue_redis: Redis,
    cache_redis: Redis,
) -> None:
    config = _config()
    await _cleanup(queue_redis, cache_redis, config)

    app = create_app(config=config, queue_redis=queue_redis, cache_redis=cache_redis)
    transport = httpx.ASGITransport(app=app)
    key = stream_key("interactive", partition_for_user(TEST_USER_ID))
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET_TOKEN}

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as client:
            first = await client.post(f"/webhook/{SECRET_PATH}", json=_update(), headers=headers)
            second = await client.post(f"/webhook/{SECRET_PATH}", json=_update(), headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200
        assert await cache_redis.exists(f"dedup:{TEST_UPDATE_ID}") == 1

        entries = await queue_redis.xrange(key, "-", "+")
        ours = [
            UpdateEnvelope.from_stream_entry(fields)
            for _, fields in entries
            if UpdateEnvelope.from_stream_entry(fields).user_id == TEST_USER_ID
        ]
        assert len(ours) == 1
        assert ours[0].update_id == TEST_UPDATE_ID
        assert ours[0].kind == "text"
    finally:
        await _cleanup(queue_redis, cache_redis, config)
