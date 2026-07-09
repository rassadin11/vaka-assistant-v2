"""Helpers for pipeline contour tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    partition_for_user,
    stream_key,
)
from core.rate_limit import rate_limit_key

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class SentUpdate:
    update_id: int
    user_id: int
    chat_id: int
    counter: int
    text: str


@dataclass(frozen=True, slots=True)
class Reply:
    entry_id: str
    chat_id: int
    text: str
    kind: str | None


@dataclass(slots=True)
class WorkerHandle:
    process: subprocess.Popen[str]
    consumer_name: str

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()

    def terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()


async def redis_or_skip(url: str) -> Redis:
    client: Redis = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except (OSError, RedisError) as exc:
        await client.aclose()
        pytest.skip(f"local dev redis is not reachable: {exc}")
    return client


@pytest.fixture
async def queue_redis() -> AsyncIterator[Redis]:
    client = await redis_or_skip(os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def cache_redis() -> AsyncIterator[Redis]:
    client = await redis_or_skip(os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL))
    try:
        yield client
    finally:
        await client.aclose()


def make_run_id() -> str:
    return uuid4().hex[:12]


def update_base() -> int:
    return int(time.time() * 1_000_000)


def make_text_update(sent: SentUpdate) -> dict[str, Any]:
    return {
        "update_id": sent.update_id,
        "message": {
            "message_id": sent.counter + 1,
            "from": {"id": sent.user_id, "is_bot": False, "first_name": "Contour"},
            "chat": {"id": sent.chat_id, "type": "private"},
            "date": 1783000000,
            "text": sent.text,
        },
    }


def make_sent_updates(
    *,
    base_update_id: int,
    base_user_id: int,
    users: int,
    per_user: int,
    text_factory: Callable[[int, int, int], str] | None = None,
) -> list[SentUpdate]:
    updates: list[SentUpdate] = []
    for user_offset in range(users):
        user_id = base_user_id + user_offset
        for counter in range(per_user):
            ordinal = user_offset * per_user + counter
            update_id = base_update_id + ordinal
            text = (
                f"u{user_id} n{counter}"
                if text_factory is None
                else text_factory(user_id, counter, update_id)
            )
            updates.append(
                SentUpdate(
                    update_id=update_id,
                    user_id=user_id,
                    chat_id=user_id,
                    counter=counter,
                    text=text,
                )
            )
    return updates


def spawn_worker(
    *,
    run_id: str,
    reply_stream: str,
    queue_url: str | None = None,
    cache_url: str | None = None,
    fast_reclaim: bool = False,
    reclaim_min_idle_ms: int | None = None,
    reclaim_interval_seconds: float | None = None,
    lock_ttl_ms: int | None = None,
    plain_echo_delay_seconds: float | None = None,
) -> WorkerHandle:
    consumer_name = f"contour-{run_id}-{uuid4().hex[:8]}"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "REDIS_QUEUE_URL": queue_url or os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL),
            "REDIS_CACHE_URL": cache_url or os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL),
            "WORKER_CONSUMER_NAME": consumer_name,
            "WORKER_INTERACTIVE_ONLY": "1",
            "WORKER_PLAIN_ECHO": "1",
            "WORKER_REPLY_STREAM": reply_stream,
            "WORKER_PROCESS_TIMEOUT_SECONDS": "30",
        }
    )
    if fast_reclaim:
        env.update(
            {
                "WORKER_RECLAIM_MIN_IDLE_MS": "1000",
                "WORKER_RECLAIM_INTERVAL_SECONDS": "1",
                "WORKER_LOCK_TTL_MS": "1000",
            }
        )
    if reclaim_min_idle_ms is not None:
        env["WORKER_RECLAIM_MIN_IDLE_MS"] = str(reclaim_min_idle_ms)
    if reclaim_interval_seconds is not None:
        env["WORKER_RECLAIM_INTERVAL_SECONDS"] = str(reclaim_interval_seconds)
    if lock_ttl_ms is not None:
        env["WORKER_LOCK_TTL_MS"] = str(lock_ttl_ms)
    if plain_echo_delay_seconds is not None:
        env["WORKER_PLAIN_ECHO_DELAY_SECONDS"] = str(plain_echo_delay_seconds)

    process = subprocess.Popen(
        [sys.executable, "-m", "worker"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return WorkerHandle(process=process, consumer_name=consumer_name)


async def wait_for_workers(handles: Iterable[WorkerHandle], *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    worker_list = list(handles)
    while time.monotonic() < deadline:
        exited = [handle for handle in worker_list if handle.process.poll() is not None]
        if exited:
            codes = [handle.process.returncode for handle in exited]
            raise AssertionError(f"worker subprocess exited early: {codes}")
        await asyncio.sleep(0.05)


async def stop_workers(handles: Iterable[WorkerHandle], *, graceful: bool) -> None:
    worker_list = list(handles)
    for handle in worker_list:
        if graceful:
            handle.terminate()
        else:
            handle.kill()
    await asyncio.gather(*[_wait_process_exit(handle) for handle in worker_list])


async def _wait_process_exit(handle: WorkerHandle) -> None:
    try:
        await asyncio.to_thread(handle.process.wait, 5)
    except subprocess.TimeoutExpired:
        handle.kill()
        await asyncio.to_thread(handle.process.wait, 5)


async def read_replies(redis: Redis, stream: str) -> list[Reply]:
    try:
        entries = await redis.xrange(stream, "-", "+")
    except ResponseError as exc:
        if "no such key" in str(exc).lower():
            return []
        raise
    replies: list[Reply] = []
    for entry_id, fields in entries:
        raw_chat_id = fields.get("chat_id")
        raw_text = fields.get("text")
        if raw_chat_id is None or raw_text is None:
            continue
        replies.append(
            Reply(
                entry_id=str(entry_id),
                chat_id=int(raw_chat_id),
                text=str(raw_text),
                kind=_optional_str(fields.get("kind")),
            )
        )
    return replies


async def wait_for_reply_keys(
    redis: Redis,
    stream: str,
    *,
    expected_keys: set[int],
    key_for_reply: Callable[[Reply], int | None],
    timeout: float = 60.0,
) -> list[Reply]:
    deadline = time.monotonic() + timeout
    last_seen = 0
    while time.monotonic() < deadline:
        replies = await read_replies(redis, stream)
        keys = [key for reply in replies if (key := key_for_reply(reply)) in expected_keys]
        if expected_keys.issubset(keys):
            return replies
        last_seen = len(keys)
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"timed out waiting for replies: seen={last_seen} expected={len(expected_keys)}"
    )


async def wait_for_count(
    predicate: Callable[[], Any],
    *,
    timeout: float = 60.0,
    interval: float = 0.1,
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")


async def wait_for_pending(
    redis: Redis,
    *,
    user_id: int,
    timeout: float = 5.0,
) -> None:
    key = stream_key("interactive", partition_for_user(user_id))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = await redis.xpending(key, CONSUMER_GROUPS["interactive"])
        if _pending_count(pending) > 0:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for pending message")


async def find_dlq_entries(
    redis: Redis,
    *,
    update_id: int,
) -> list[tuple[str, dict[str, str]]]:
    entries = await redis.xrange(DLQ_STREAM, "-", "+")
    found: list[tuple[str, dict[str, str]]] = []
    for entry_id, fields in entries:
        try:
            envelope = UpdateEnvelope.from_stream_entry(fields)
        except ValueError:
            continue
        if envelope.update_id == update_id:
            found.append((str(entry_id), fields))
    return found


async def cleanup_test_state(
    *,
    queue_redis: Redis,
    cache_redis: Redis,
    user_ids: Iterable[int],
    update_ids: Iterable[int],
    reply_stream: str,
) -> None:
    user_id_set = set(user_ids)
    update_id_set = set(update_ids)
    await _delete_stream_entries(queue_redis, user_id_set=user_id_set, update_id_set=update_id_set)
    await _delete_dlq_entries(queue_redis, user_id_set=user_id_set, update_id_set=update_id_set)
    queue_keys = [f"lock:user:{user_id}" for user_id in user_id_set]
    cache_keys = [
        key
        for update_id in update_id_set
        for key in (f"dedup:{update_id}", f"dedup:worker:{update_id}")
    ]
    cache_keys.extend(rate_limit_key(user_id) for user_id in user_id_set)
    cache_keys.extend(f"rl:warned:{user_id}" for user_id in user_id_set)
    if queue_keys:
        await queue_redis.delete(*queue_keys)
    if cache_keys:
        await cache_redis.delete(*cache_keys)
    await queue_redis.delete(reply_stream)


async def _delete_stream_entries(
    redis: Redis,
    *,
    user_id_set: set[int],
    update_id_set: set[int],
) -> None:
    for queue in ("interactive", "background"):
        queue_name = _queue_name(queue)
        for user_id in user_id_set:
            key = stream_key(queue_name, partition_for_user(user_id))
            entries = await redis.xrange(key, "-", "+")
            entry_ids: list[str] = []
            for entry_id, fields in entries:
                try:
                    envelope = UpdateEnvelope.from_stream_entry(fields)
                except ValueError:
                    continue
                if envelope.user_id in user_id_set or envelope.update_id in update_id_set:
                    entry_ids.append(str(entry_id))
            if entry_ids:
                with suppress(ResponseError):
                    await redis.xack(key, CONSUMER_GROUPS[queue_name], *entry_ids)
                await redis.xdel(key, *entry_ids)


async def _delete_dlq_entries(
    redis: Redis,
    *,
    user_id_set: set[int],
    update_id_set: set[int],
) -> None:
    entries = await redis.xrange(DLQ_STREAM, "-", "+")
    entry_ids: list[str] = []
    for entry_id, fields in entries:
        try:
            envelope = UpdateEnvelope.from_stream_entry(fields)
        except ValueError:
            continue
        if envelope.user_id in user_id_set or envelope.update_id in update_id_set:
            entry_ids.append(str(entry_id))
    if entry_ids:
        await redis.xdel(DLQ_STREAM, *entry_ids)


def _queue_name(value: str) -> QueueName:
    if value == "interactive":
        return "interactive"
    if value == "background":
        return "background"
    raise AssertionError(f"unexpected queue: {value}")


def _pending_count(pending: object) -> int:
    if isinstance(pending, dict):
        raw_value = pending.get("pending")
        if raw_value is None:
            raw_value = pending.get("count")
        return int(raw_value or 0)
    if isinstance(pending, list | tuple) and pending:
        return int(pending[0])
    return 0


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
