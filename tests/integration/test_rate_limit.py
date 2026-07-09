"""Integration tests for the Redis Lua token bucket."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.queue import DEFAULT_REDIS_CACHE_URL
from core.rate_limit import allow_user_update, rate_limit_key

pytestmark = pytest.mark.integration

TEST_USER_ID = 990_000_303


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
        await client.delete(rate_limit_key(TEST_USER_ID))
        await client.aclose()


async def test_lua_token_bucket_bursts_denies_and_refills(cache_redis: Redis) -> None:
    await cache_redis.delete(rate_limit_key(TEST_USER_ID))

    assert await allow_user_update(
        cache_redis,
        TEST_USER_ID,
        per_minute=2,
        burst=2,
        refill_window_ms=1_000,
        now_ms=1_000_000,
    )
    assert await allow_user_update(
        cache_redis,
        TEST_USER_ID,
        per_minute=2,
        burst=2,
        refill_window_ms=1_000,
        now_ms=1_000_000,
    )
    assert not await allow_user_update(
        cache_redis,
        TEST_USER_ID,
        per_minute=2,
        burst=2,
        refill_window_ms=1_000,
        now_ms=1_000_000,
    )
    assert await allow_user_update(
        cache_redis,
        TEST_USER_ID,
        per_minute=2,
        burst=2,
        refill_window_ms=1_000,
        now_ms=1_000_500,
    )
    assert not await allow_user_update(
        cache_redis,
        TEST_USER_ID,
        per_minute=2,
        burst=2,
        refill_window_ms=1_000,
        now_ms=1_000_500,
    )
