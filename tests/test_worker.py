"""Unit tests for worker queue reliability behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from core.envelope import UpdateEnvelope
from core.queue import CONSUMER_GROUPS, DLQ_STREAM, QueueName, stream_key
from worker.app import Worker, WorkerConfig
from worker.processor import Processor


def _envelope(**overrides: object) -> UpdateEnvelope:
    defaults: dict[str, object] = {
        "update_id": 100,
        "user_id": 42,
        "chat_id": 42,
        "kind": "text",
        "payload": {"text": "hello"},
    }
    defaults.update(overrides)
    return UpdateEnvelope.model_validate(defaults)


def _read_response(queue: QueueName, envelope: UpdateEnvelope, entry_id: str = "1-0") -> object:
    return [[stream_key(queue, 0), [(entry_id, envelope.to_stream_entry())]]]


class FakeQueueRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.pending_counts: dict[tuple[str, str], int] = {}
        self.read_responses: dict[QueueName, list[object]] = {
            "interactive": [],
            "background": [],
        }
        self.read_calls: list[tuple[str, int | None]] = []
        self.acked: list[tuple[str, str, str]] = []
        self.dlq_entries: list[tuple[str, dict[str, str]]] = []
        self.groups_created: list[tuple[str, str]] = []
        self.autoclaim_responses: dict[str, list[tuple[str, dict[str, str]]]] = {}

    async def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool = False,
        px: int | None = None,
    ) -> bool:
        del px
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        del numkeys
        key = str(keys_and_args[0])
        token = str(keys_and_args[1])
        if self.values.get(key) != token:
            return 0
        if "DEL" in script:
            del self.values[key]
            return 1
        if "PEXPIRE" in script:
            return 1
        raise AssertionError(f"unexpected script: {script}")

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        *,
        count: int,
        block: int | None,
    ) -> object:
        del consumername, streams, count
        queue = _queue_for_group(groupname)
        self.read_calls.append((queue, block))
        responses = self.read_responses[queue]
        if responses:
            return responses.pop(0)
        return []

    async def xpending_range(
        self,
        name: str,
        groupname: str,
        *,
        min: str,
        max: str,
        count: int,
    ) -> list[dict[str, int]]:
        del groupname, max, count
        return [{"times_delivered": self.pending_counts.get((name, min), 1)}]

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        entry_id = ids[0]
        self.acked.append((name, groupname, entry_id))
        return 1

    async def xadd(self, name: str, fields: dict[str, str], **kwargs: Any) -> str:
        del kwargs
        if name == DLQ_STREAM:
            self.dlq_entries.append((name, fields))
        return "9-0"

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        *,
        start_id: str,
        count: int,
    ) -> object:
        del groupname, consumername, min_idle_time, start_id, count
        entries = self.autoclaim_responses.pop(name, [])
        return ["0-0", entries]

    async def xgroup_create(self, name: str, groupname: str, *, id: str, mkstream: bool) -> None:
        del id, mkstream
        self.groups_created.append((name, groupname))


class FakeCacheRedis:
    def __init__(self, *, existing: set[str] | None = None) -> None:
        self.existing = existing if existing is not None else set()
        self.set_calls: list[tuple[str, str, int | None, bool]] = []

    def exists(self, name: str) -> Awaitable[int]:
        async def _exists() -> int:
            return int(name in self.existing)

        return _exists()

    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> Awaitable[object]:
        async def _set() -> object:
            self.set_calls.append((name, value, ex, nx))
            if nx and name in self.existing:
                return False
            self.existing.add(name)
            return True

        return _set()


class RecordingProcessor:
    def __init__(self) -> None:
        self.envelopes: list[UpdateEnvelope] = []

    async def process(self, envelope: UpdateEnvelope) -> str | None:
        self.envelopes.append(envelope)
        text = envelope.payload.get("text")
        return text if isinstance(text, str) else None


class BlockingProcessor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process(self, envelope: UpdateEnvelope) -> str | None:
        self.started.set()
        await self.release.wait()
        text = envelope.payload.get("text")
        return text if isinstance(text, str) else None


class CallbackRecorder:
    def __init__(self) -> None:
        self.replies: list[tuple[int, str]] = []
        self.typing: list[int] = []
        self.admin: list[str] = []

    async def send_reply(self, chat_id: int, text: str) -> None:
        self.replies.append((chat_id, text))

    async def send_typing(self, chat_id: int) -> None:
        self.typing.append(chat_id)

    async def notify_admin(self, text: str) -> None:
        self.admin.append(text)


def _worker(
    queue_redis: FakeQueueRedis,
    cache_redis: FakeCacheRedis,
    processor: Processor | None = None,
    callbacks: CallbackRecorder | None = None,
    config: WorkerConfig | None = None,
) -> tuple[Worker, CallbackRecorder]:
    local_callbacks = callbacks if callbacks is not None else CallbackRecorder()
    return (
        Worker(
            queue_redis=queue_redis,
            cache_redis=cache_redis,
            processor=processor if processor is not None else RecordingProcessor(),
            send_reply=local_callbacks.send_reply,
            send_typing=local_callbacks.send_typing,
            notify_admin=local_callbacks.notify_admin,
            config=config
            if config is not None
            else WorkerConfig(reclaim_interval_seconds=999, lock_retry_sleep_seconds=0),
        ),
        local_callbacks,
    )


async def test_priority_reads_interactive_before_background() -> None:
    queue_redis = FakeQueueRedis()
    envelope = _envelope()
    queue_redis.read_responses["interactive"].append(_read_response("interactive", envelope))
    queue_redis.read_responses["background"].append(_read_response("background", envelope))
    worker, callbacks = _worker(queue_redis, FakeCacheRedis())

    assert await worker.run_once()

    assert queue_redis.read_calls == [("interactive", None)]
    assert callbacks.replies == [(42, "hello")]
    assert queue_redis.acked == [
        (stream_key("interactive", 0), CONSUMER_GROUPS["interactive"], "1-0")
    ]


async def test_priority_reads_background_only_after_interactive_empty() -> None:
    queue_redis = FakeQueueRedis()
    queue_redis.read_responses["background"].append(_read_response("background", _envelope()))
    worker, callbacks = _worker(queue_redis, FakeCacheRedis())

    assert await worker.run_once()

    assert queue_redis.read_calls == [("interactive", None), ("background", 2000)]
    assert callbacks.replies == [(42, "hello")]


async def test_interactive_only_worker_blocks_on_interactive_queue() -> None:
    queue_redis = FakeQueueRedis()
    worker, _callbacks = _worker(
        queue_redis,
        FakeCacheRedis(),
        config=WorkerConfig(
            interactive_only=True,
            reclaim_interval_seconds=999,
            lock_retry_sleep_seconds=0,
        ),
    )

    assert not await worker.run_once()

    assert queue_redis.read_calls == [("interactive", 2000)]


async def test_duplicate_update_is_acknowledged_and_skipped() -> None:
    queue_redis = FakeQueueRedis()
    envelope = _envelope(update_id=123)
    queue_redis.read_responses["interactive"].append(_read_response("interactive", envelope))
    worker, callbacks = _worker(
        queue_redis,
        FakeCacheRedis(existing={"dedup:worker:123"}),
    )

    assert await worker.run_once()

    assert callbacks.replies == []
    assert queue_redis.acked == [
        (stream_key("interactive", 0), CONSUMER_GROUPS["interactive"], "1-0")
    ]


async def test_delivery_limit_moves_message_to_dlq_and_notifies() -> None:
    queue_redis = FakeQueueRedis()
    envelope = _envelope(update_id=456)
    stream = stream_key("interactive", 0)
    queue_redis.read_responses["interactive"].append(_read_response("interactive", envelope))
    queue_redis.pending_counts[(stream, "1-0")] = 3
    processor = RecordingProcessor()
    worker, callbacks = _worker(queue_redis, FakeCacheRedis(), processor=processor)

    assert await worker.run_once()

    assert processor.envelopes == []
    assert len(queue_redis.dlq_entries) == 1
    _dlq_stream, dlq_fields = queue_redis.dlq_entries[0]
    assert dlq_fields["source_stream"] == stream
    assert dlq_fields["source_entry_id"] == "1-0"
    assert dlq_fields["delivery_count"] == "3"
    assert queue_redis.acked == [(stream, CONSUMER_GROUPS["interactive"], "1-0")]
    expected_reply = "Не получилось обработать запрос, разбираемся."  # noqa: RUF001
    assert callbacks.replies == [(42, expected_reply)]
    assert callbacks.admin


async def test_lock_contention_leaves_message_pending_without_ack() -> None:
    queue_redis = FakeQueueRedis()
    queue_redis.values["lock:user:42"] = "other-worker"
    queue_redis.read_responses["interactive"].append(_read_response("interactive", _envelope()))
    worker, callbacks = _worker(queue_redis, FakeCacheRedis())

    assert await worker.run_once()

    assert callbacks.replies == []
    assert queue_redis.acked == []


async def test_multi_partition_read_processes_every_delivered_message() -> None:
    queue_redis = FakeQueueRedis()
    first = _envelope(update_id=301, user_id=42, chat_id=42)
    second = _envelope(update_id=302, user_id=43, chat_id=43)
    queue_redis.read_responses["interactive"].append(
        [
            [stream_key("interactive", 0), [("1-0", first.to_stream_entry())]],
            [stream_key("interactive", 5), [("2-0", second.to_stream_entry())]],
        ]
    )
    processor = RecordingProcessor()
    worker, callbacks = _worker(queue_redis, FakeCacheRedis(), processor=processor)

    assert await worker.run_once()

    assert [envelope.update_id for envelope in processor.envelopes] == [301, 302]
    assert callbacks.replies == [(42, "hello"), (43, "hello")]
    assert {entry_id for _, _, entry_id in queue_redis.acked} == {"1-0", "2-0"}


async def test_reclaim_processes_every_claimed_message() -> None:
    queue_redis = FakeQueueRedis()
    stream = stream_key("interactive", 0)
    first = _envelope(update_id=201, user_id=42, chat_id=42)
    second = _envelope(update_id=202, user_id=43, chat_id=43)
    queue_redis.autoclaim_responses[stream] = [
        ("5-0", first.to_stream_entry()),
        ("6-0", second.to_stream_entry()),
    ]
    queue_redis.pending_counts[(stream, "5-0")] = 2
    queue_redis.pending_counts[(stream, "6-0")] = 2
    processor = RecordingProcessor()
    worker, callbacks = _worker(
        queue_redis,
        FakeCacheRedis(),
        processor=processor,
        config=WorkerConfig(reclaim_interval_seconds=0, lock_retry_sleep_seconds=0),
    )

    assert await worker.run_once()

    assert [envelope.update_id for envelope in processor.envelopes] == [201, 202]
    assert [envelope.attempt for envelope in processor.envelopes] == [2, 2]
    assert callbacks.replies == [(42, "hello"), (43, "hello")]
    assert {entry_id for _, _, entry_id in queue_redis.acked} == {"5-0", "6-0"}


async def test_graceful_shutdown_finishes_in_flight_message() -> None:
    queue_redis = FakeQueueRedis()
    queue_redis.read_responses["interactive"].append(_read_response("interactive", _envelope()))
    processor = BlockingProcessor()
    callbacks = CallbackRecorder()
    worker, _callbacks = _worker(
        queue_redis,
        FakeCacheRedis(),
        processor=processor,
        callbacks=callbacks,
    )

    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(processor.started.wait(), timeout=1)
    worker.request_stop()
    processor.release.set()
    await asyncio.wait_for(task, timeout=1)

    assert callbacks.replies == [(42, "hello")]
    assert queue_redis.acked == [
        (stream_key("interactive", 0), CONSUMER_GROUPS["interactive"], "1-0")
    ]


def _queue_for_group(groupname: str) -> QueueName:
    for queue, group in CONSUMER_GROUPS.items():
        if group == groupname:
            return queue
    raise AssertionError(f"unexpected group: {groupname}")
