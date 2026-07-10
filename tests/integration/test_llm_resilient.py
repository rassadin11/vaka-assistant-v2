"""Integration tests for the Redis Lua LLM semaphore."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.llm_mock import MockLLMProvider
from core.llm_resilient import SEMAPHORE_KEY, ResilientLLMConfig, ResilientLLMProvider
from core.queue import DEFAULT_REDIS_QUEUE_URL

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
async def queue_redis() -> AsyncIterator[Redis]:
    client = await _redis_or_skip(os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL))
    try:
        yield client
    finally:
        await client.delete(SEMAPHORE_KEY)
        await client.aclose()


async def test_lua_semaphore_honors_limit_sets_ttl_and_releases(queue_redis: Redis) -> None:
    config = ResilientLLMConfig(max_concurrency=2, semaphore_wait_seconds=1)
    providers = [
        ResilientLLMProvider(MockLLMProvider.scripted([]), queue_redis, "test/model", config=config)
        for _ in range(3)
    ]

    await asyncio.gather(*(provider._acquire_semaphore() for provider in providers[:2]))
    assert await queue_redis.get(SEMAPHORE_KEY) == "2"
    assert 0 < await queue_redis.ttl(SEMAPHORE_KEY) <= config.semaphore_ttl_seconds

    third_acquired = asyncio.create_task(providers[2]._acquire_semaphore())
    await asyncio.sleep(0.05)
    assert not third_acquired.done()

    await providers[0]._release_semaphore()
    await third_acquired
    assert await queue_redis.get(SEMAPHORE_KEY) == "2"

    await providers[1]._release_semaphore()
    await providers[2]._release_semaphore()
    assert await queue_redis.get(SEMAPHORE_KEY) == "0"
