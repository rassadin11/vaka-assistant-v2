"""Integration tests for worker processing and Redis Stream reclaim."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from core.envelope import UpdateEnvelope
from core.queue import (
    CONSUMER_GROUPS,
    DEFAULT_REDIS_CACHE_URL,
    DEFAULT_REDIS_QUEUE_URL,
    DLQ_STREAM,
    QueueName,
    decode_read_group_response,
    enqueue,
    partition_for_user,
    reclaim_stale_pending,
    stream_key,
    stream_keys,
)
from worker.app import Worker, WorkerConfig
from worker.processor import EchoProcessor

pytestmark = pytest.mark.integration

TEST_USER_ID = 990_000_202


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


def _envelope(update_id: int, text: str = "hello") -> UpdateEnvelope:
    return UpdateEnvelope(
        update_id=update_id,
        user_id=TEST_USER_ID,
        chat_id=TEST_USER_ID,
        kind="text",
        payload={"text": text},
    )


async def test_enqueue_then_worker_echoes_reply(
    queue_redis: Redis,
    cache_redis: Redis,
) -> None:
    update_id = int(time.time() * 1000) % 1_000_000_000
    group_name = f"g:test:worker:{update_id}"
    queue = "interactive"
    key = stream_key(queue, partition_for_user(TEST_USER_ID))
    replies: list[tuple[int, str]] = []
    typing: list[int] = []
    admin: list[str] = []
    old_group = CONSUMER_GROUPS[queue]
    entry_id: str | None = None

    async def send_reply(chat_id: int, text: str) -> None:
        replies.append((chat_id, text))

    async def send_typing(chat_id: int) -> None:
        typing.append(chat_id)

    async def notify_admin(text: str) -> None:
        admin.append(text)

    CONSUMER_GROUPS[queue] = group_name
    try:
        await _create_group(queue_redis, queue, group_name)
        envelope = _envelope(update_id, text="echo me")
        entry_id = await enqueue(queue_redis, queue, envelope)
        response = await queue_redis.xreadgroup(
            group_name,
            "test-consumer",
            streams={key: ">"},
            count=1,
            block=1000,
        )
        messages = decode_read_group_response(response, queue)
        assert [message.entry_id for message in messages] == [entry_id]

        worker = Worker(
            queue_redis=queue_redis,
            cache_redis=cache_redis,
            processor=EchoProcessor(),
            send_reply=send_reply,
            send_typing=send_typing,
            notify_admin=notify_admin,
            config=WorkerConfig(
                consumer_name="test-consumer",
                reclaim_interval_seconds=999,
                lock_retry_sleep_seconds=0,
            ),
        )
        await worker._handle_message(messages[0])

        assert replies == [(TEST_USER_ID, "echo me")]
        assert typing == [TEST_USER_ID]
        assert admin == []
        assert await cache_redis.exists(f"dedup:worker:{update_id}") == 1
    finally:
        CONSUMER_GROUPS[queue] = old_group
        if entry_id is not None:
            await queue_redis.xdel(key, entry_id)
        await cache_redis.delete(f"dedup:worker:{update_id}")
        await queue_redis.delete(f"lock:user:{TEST_USER_ID}")
        await _destroy_group(queue_redis, queue, group_name)


async def test_reclaim_abandoned_pending_entry(queue_redis: Redis) -> None:
    update_id = int(time.time() * 1000) % 1_000_000_000 + 1
    group_name = f"g:test:reclaim:{update_id}"
    queue = "interactive"
    key = stream_key(queue, partition_for_user(TEST_USER_ID))
    old_group = CONSUMER_GROUPS[queue]
    entry_id: str | None = None

    CONSUMER_GROUPS[queue] = group_name
    try:
        await _create_group(queue_redis, queue, group_name)
        entry_id = await enqueue(queue_redis, queue, _envelope(update_id))
        response = await queue_redis.xreadgroup(
            group_name,
            "abandoned-consumer",
            streams={key: ">"},
            count=1,
            block=1000,
        )
        messages = decode_read_group_response(response, queue)
        assert [message.entry_id for message in messages] == [entry_id]

        reclaimed = await reclaim_stale_pending(
            queue_redis,
            queue,
            "new-consumer",
            min_idle_ms=0,
            count=10,
        )

        ours = [message for message in reclaimed if message.entry_id == entry_id]
        assert len(ours) == 1
        assert ours[0].envelope.update_id == update_id
        assert ours[0].delivery_count >= 2
    finally:
        CONSUMER_GROUPS[queue] = old_group
        if entry_id is not None:
            await queue_redis.xack(key, group_name, entry_id)
            await queue_redis.xdel(key, entry_id)
        await _destroy_group(queue_redis, queue, group_name)
        await queue_redis.delete(DLQ_STREAM)


async def _create_group(redis: Redis, queue: QueueName, group_name: str) -> None:
    for key in stream_keys(queue):
        try:
            await redis.xgroup_create(key, group_name, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise


async def _destroy_group(redis: Redis, queue: QueueName, group_name: str) -> None:
    for key in stream_keys(queue):
        with suppress(ResponseError):
            await redis.xgroup_destroy(key, group_name)
