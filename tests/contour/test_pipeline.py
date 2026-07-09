"""End-to-end reliability contour tests for the gateway/queue/worker pipeline."""

from __future__ import annotations

import asyncio
import os
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis

from core.queue import (
    DEFAULT_REDIS_CACHE_URL,
    DEFAULT_REDIS_QUEUE_URL,
    USER_DLQ_MESSAGE,
    RedisSettings,
)
from gateway.app import create_app
from gateway.config import GatewayConfig
from tests.contour import helpers as h

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SECRET_PATH = "contour-path"
SECRET_TOKEN = "contour-token"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET_TOKEN}


@pytest.fixture
async def queue_redis() -> AsyncIterator[Redis]:
    client = await h.redis_or_skip(os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def cache_redis() -> AsyncIterator[Redis]:
    client = await h.redis_or_skip(os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL))
    try:
        yield client
    finally:
        await client.aclose()


async def test_load_preserves_per_user_order_with_four_workers(
    queue_redis: Redis,
    cache_redis: Redis,
) -> None:
    run_id = h.make_run_id()
    reply_stream = f"test:reply:{run_id}:load"
    updates = h.make_sent_updates(
        base_update_id=h.update_base(),
        base_user_id=991_000_000,
        users=20,
        per_user=50,
    )
    workers: list[h.WorkerHandle] = []

    await h.cleanup_test_state(
        queue_redis=queue_redis,
        cache_redis=cache_redis,
        user_ids=[sent.user_id for sent in updates],
        update_ids=[sent.update_id for sent in updates],
        reply_stream=reply_stream,
    )
    try:
        workers = [h.spawn_worker(run_id=run_id, reply_stream=reply_stream) for _ in range(4)]
        await h.wait_for_workers(workers, timeout=0.5)

        await _post_load_updates(queue_redis, cache_redis, updates)
        replies = await h.wait_for_reply_keys(
            queue_redis,
            reply_stream,
            expected_keys={sent.update_id for sent in updates},
            key_for_reply=_reply_key_for(updates),
            timeout=60,
        )

        matched = _assert_exactly_once_replies(replies, updates)
        _assert_per_user_order(matched, updates)
    finally:
        await h.stop_workers(workers, graceful=False)
        await h.cleanup_test_state(
            queue_redis=queue_redis,
            cache_redis=cache_redis,
            user_ids=[sent.user_id for sent in updates],
            update_ids=[sent.update_id for sent in updates],
            reply_stream=reply_stream,
        )


async def test_chaos_kill_reclaims_in_flight_message_exactly_once(
    queue_redis: Redis,
    cache_redis: Redis,
) -> None:
    run_id = h.make_run_id()
    reply_stream = f"test:reply:{run_id}:chaos"
    user_id = 991_100_000
    updates = h.make_sent_updates(
        base_update_id=h.update_base(),
        base_user_id=user_id,
        users=1,
        per_user=24,
        text_factory=lambda _user_id, counter, update_id: f"chaos {update_id} n{counter}",
    )
    workers: list[h.WorkerHandle] = []

    await h.cleanup_test_state(
        queue_redis=queue_redis,
        cache_redis=cache_redis,
        user_ids=[user_id],
        update_ids=[sent.update_id for sent in updates],
        reply_stream=reply_stream,
    )
    try:
        slow_worker = h.spawn_worker(
            run_id=run_id,
            reply_stream=reply_stream,
            lock_ttl_ms=1000,
            plain_echo_delay_seconds=5,
        )
        workers = [slow_worker]
        await h.wait_for_workers(workers, timeout=0.5)
        await _post_updates(queue_redis, cache_redis, updates)

        await h.wait_for_pending(queue_redis, user_id=user_id, timeout=15)
        slow_worker.kill()
        await h.stop_workers([slow_worker], graceful=False)
        workers = [h.spawn_worker(run_id=run_id, reply_stream=reply_stream, fast_reclaim=True)]
        await h.wait_for_workers(workers, timeout=0.5)

        replies = await h.wait_for_reply_keys(
            queue_redis,
            reply_stream,
            expected_keys={sent.update_id for sent in updates},
            key_for_reply=_reply_key_for(updates),
            timeout=60,
        )
        _assert_exactly_once_replies(replies, updates)
    finally:
        await h.stop_workers(workers, graceful=False)
        await h.cleanup_test_state(
            queue_redis=queue_redis,
            cache_redis=cache_redis,
            user_ids=[user_id],
            update_ids=[sent.update_id for sent in updates],
            reply_stream=reply_stream,
        )


async def test_poison_message_moves_to_dlq_and_partition_continues(
    queue_redis: Redis,
    cache_redis: Redis,
) -> None:
    run_id = h.make_run_id()
    reply_stream = f"test:reply:{run_id}:poison"
    user_id = 991_200_000
    base_update_id = h.update_base()
    poison = h.SentUpdate(
        update_id=base_update_id,
        user_id=user_id,
        chat_id=user_id,
        counter=0,
        text="__poison__",
    )
    normal = h.make_sent_updates(
        base_update_id=base_update_id + 1,
        base_user_id=user_id,
        users=1,
        per_user=4,
        text_factory=lambda _user_id, counter, update_id: f"after-poison {update_id} n{counter}",
    )
    updates = [poison, *normal]
    workers: list[h.WorkerHandle] = []

    await h.cleanup_test_state(
        queue_redis=queue_redis,
        cache_redis=cache_redis,
        user_ids=[user_id],
        update_ids=[sent.update_id for sent in updates],
        reply_stream=reply_stream,
    )
    try:
        workers = [h.spawn_worker(run_id=run_id, reply_stream=reply_stream, fast_reclaim=True)]
        await h.wait_for_workers(workers, timeout=0.5)
        await _post_updates(queue_redis, cache_redis, [poison])

        poison_result = await h.wait_for_count(
            lambda: _poison_done(queue_redis, reply_stream, poison.update_id),
            timeout=60,
        )
        dlq_replies, dlq_entries = poison_result
        await _post_updates(queue_redis, cache_redis, normal)
        normal_replies = await h.wait_for_reply_keys(
            queue_redis,
            reply_stream,
            expected_keys={sent.update_id for sent in normal},
            key_for_reply=_reply_key_for(normal),
            timeout=30,
        )
        replies = [*dlq_replies, *normal_replies]
        matched = _assert_exactly_once_replies(replies, normal)
        assert [sent.text for sent in matched] == [sent.text for sent in normal]
        assert len(dlq_entries) == 1
        assert dlq_entries[0][1]["delivery_count"] == "3"
        assert any(reply.chat_id == user_id and reply.text == USER_DLQ_MESSAGE for reply in replies)
        assert any(
            reply.chat_id == 0 and reply.kind == "admin" and str(poison.update_id) in reply.text
            for reply in replies
        )
    finally:
        await h.stop_workers(workers, graceful=False)
        await h.cleanup_test_state(
            queue_redis=queue_redis,
            cache_redis=cache_redis,
            user_ids=[user_id],
            update_ids=[sent.update_id for sent in updates],
            reply_stream=reply_stream,
        )


async def test_rolling_restart_while_enqueuing_loses_no_messages(
    queue_redis: Redis,
    cache_redis: Redis,
) -> None:
    run_id = h.make_run_id()
    reply_stream = f"test:reply:{run_id}:restart"
    updates = h.make_sent_updates(
        base_update_id=h.update_base(),
        base_user_id=991_300_000,
        users=20,
        per_user=12,
        text_factory=lambda user_id, counter, update_id: (
            f"restart {update_id} u{user_id} n{counter}"
        ),
    )
    workers: list[h.WorkerHandle] = []
    sent_count = 0

    await h.cleanup_test_state(
        queue_redis=queue_redis,
        cache_redis=cache_redis,
        user_ids=[sent.user_id for sent in updates],
        update_ids=[sent.update_id for sent in updates],
        reply_stream=reply_stream,
    )
    try:
        workers = [
            h.spawn_worker(run_id=run_id, reply_stream=reply_stream, fast_reclaim=True)
            for _ in range(4)
        ]
        await h.wait_for_workers(workers, timeout=0.5)

        async def produce() -> None:
            nonlocal sent_count
            async with _gateway_client(queue_redis, cache_redis) as client:
                for sent in updates:
                    await _post_update(client, sent)
                    sent_count += 1
                    await asyncio.sleep(0.01)

        producer = asyncio.create_task(produce())
        await h.wait_for_count(lambda: _sent_at_least(sent_count, 60), timeout=15, interval=0.05)
        await h.stop_workers(workers, graceful=True)
        workers = [
            h.spawn_worker(run_id=run_id, reply_stream=reply_stream, fast_reclaim=True)
            for _ in range(4)
        ]
        await h.wait_for_workers(workers, timeout=0.5)
        await producer

        replies = await h.wait_for_reply_keys(
            queue_redis,
            reply_stream,
            expected_keys={sent.update_id for sent in updates},
            key_for_reply=_reply_key_for(updates),
            timeout=60,
        )
        _assert_exactly_once_replies(replies, updates)
    finally:
        await h.stop_workers(workers, graceful=False)
        await h.cleanup_test_state(
            queue_redis=queue_redis,
            cache_redis=cache_redis,
            user_ids=[sent.user_id for sent in updates],
            update_ids=[sent.update_id for sent in updates],
            reply_stream=reply_stream,
        )


async def _post_load_updates(
    queue_redis: Redis,
    cache_redis: Redis,
    updates: list[h.SentUpdate],
) -> None:
    by_user: dict[int, list[h.SentUpdate]] = defaultdict(list)
    for sent in updates:
        by_user[sent.user_id].append(sent)

    async with _gateway_client(queue_redis, cache_redis) as client:
        await asyncio.gather(
            *[_post_user_at_rate(client, user_updates) for user_updates in by_user.values()]
        )


async def _post_user_at_rate(
    client: httpx.AsyncClient,
    updates: list[h.SentUpdate],
) -> None:
    started_at = asyncio.get_running_loop().time()
    for index, sent in enumerate(updates):
        await _post_update(client, sent)
        target = started_at + ((index + 1) * 0.2)
        await asyncio.sleep(max(0, target - asyncio.get_running_loop().time()))


async def _post_updates(
    queue_redis: Redis,
    cache_redis: Redis,
    updates: list[h.SentUpdate],
) -> None:
    async with _gateway_client(queue_redis, cache_redis) as client:
        for sent in updates:
            await _post_update(client, sent)


def _gateway_client(
    queue_redis: Redis,
    cache_redis: Redis,
) -> httpx.AsyncClient:
    app = create_app(
        config=_gateway_config(),
        queue_redis=queue_redis,
        cache_redis=cache_redis,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://contour.test",
    )


async def _post_update(client: httpx.AsyncClient, sent: h.SentUpdate) -> None:
    response = await client.post(
        f"/webhook/{SECRET_PATH}",
        json=h.make_text_update(sent),
        headers=HEADERS,
    )
    assert response.status_code == 200


def _gateway_config() -> GatewayConfig:
    return GatewayConfig(
        webhook_secret_path=SECRET_PATH,
        telegram_webhook_secret_token=SECRET_TOKEN,
        redis=RedisSettings(
            queue_url=os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL),
            cache_url=os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL),
        ),
        port=8000,
        public_url=None,
        admin_ids=(),
        rate_limit_per_minute=10_000,
        rate_limit_burst=10_000,
    )


def _reply_key_for(updates: list[h.SentUpdate]) -> Any:
    text_to_update_id = {sent.text: sent.update_id for sent in updates}

    def key_for_reply(reply: h.Reply) -> int | None:
        return text_to_update_id.get(reply.text)

    return key_for_reply


def _assert_exactly_once_replies(
    replies: list[h.Reply],
    updates: list[h.SentUpdate],
) -> list[h.SentUpdate]:
    sent_by_text = {sent.text: sent for sent in updates}
    expected_update_ids = {sent.update_id for sent in updates}
    matched = [sent_by_text[reply.text] for reply in replies if reply.text in sent_by_text]
    counts = Counter(sent.update_id for sent in matched)
    assert set(counts) == expected_update_ids
    assert all(count == 1 for count in counts.values())
    assert len(matched) == len(updates)
    return matched


def _assert_per_user_order(
    matched: list[h.SentUpdate],
    updates: list[h.SentUpdate],
) -> None:
    expected_by_user: dict[int, list[int]] = defaultdict(list)
    actual_by_user: dict[int, list[int]] = defaultdict(list)
    for sent in updates:
        expected_by_user[sent.user_id].append(sent.counter)
    for sent in matched:
        actual_by_user[sent.user_id].append(sent.counter)
    assert actual_by_user == expected_by_user


async def _poison_done(
    redis: Redis,
    reply_stream: str,
    poison_update_id: int,
) -> tuple[list[h.Reply], list[tuple[str, dict[str, str]]]] | None:
    replies = await h.read_replies(redis, reply_stream)
    dlq_entries = await h.find_dlq_entries(redis, update_id=poison_update_id)
    has_dlq_notice = any(reply.text == USER_DLQ_MESSAGE for reply in replies)
    if dlq_entries and has_dlq_notice:
        return replies, dlq_entries
    return None


async def _sent_at_least(sent_count: int, target: int) -> bool:
    return sent_count >= target
