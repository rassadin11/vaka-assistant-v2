"""Redis Streams queue helpers.

Streams are partitioned by Telegram user id. Changing ``PARTITION_COUNT`` after
data has been enqueued requires a deliberate rebalance because per-user order is
derived from this modulo.
"""

from __future__ import annotations

import os
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from core.envelope import UpdateEnvelope

QueueName = Literal["interactive", "background"]

DEFAULT_REDIS_QUEUE_URL = "redis://localhost:6379/0"
DEFAULT_REDIS_CACHE_URL = "redis://localhost:6380/0"
PARTITION_COUNT = 16
STREAM_MAXLEN = 100_000
DLQ_STREAM = "q:dlq"
DEFAULT_RECLAIM_MIN_IDLE_MS = 210_000
DEFAULT_MAX_DELIVERIES = 3
USER_DLQ_MESSAGE = "Не получилось обработать запрос, разбираемся."  # noqa: RUF001
CONSUMER_GROUPS: dict[QueueName, str] = {
    "interactive": "g:interactive",
    "background": "g:background",
}

SendReplyCallback = Callable[[int, str], Awaitable[None]]
NotifyAdminCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RedisSettings:
    """Redis connection URLs loaded from environment variables."""

    queue_url: str
    cache_url: str


@dataclass(frozen=True, slots=True)
class QueueMessage:
    """A decoded Redis Stream message with its source coordinates."""

    queue: QueueName
    stream: str
    entry_id: str
    envelope: UpdateEnvelope
    delivery_count: int = 1


def redis_settings_from_env() -> RedisSettings:
    """Load Redis connection settings with local compose defaults."""

    return RedisSettings(
        queue_url=os.getenv("REDIS_QUEUE_URL", DEFAULT_REDIS_QUEUE_URL),
        cache_url=os.getenv("REDIS_CACHE_URL", DEFAULT_REDIS_CACHE_URL),
    )


def partition_for_user(user_id: int) -> int:
    """Return the stable stream partition for a Telegram user id."""

    return zlib.crc32(str(user_id).encode("ascii")) % PARTITION_COUNT


def stream_key(queue: QueueName, partition: int) -> str:
    """Return a Redis Stream key for a queue partition."""

    if partition < 0 or partition >= PARTITION_COUNT:
        raise ValueError("partition is out of range.")
    return f"q:{queue}:{partition}"


def stream_keys(queue: QueueName) -> list[str]:
    """Return all stream keys for a queue."""

    return [stream_key(queue, partition) for partition in range(PARTITION_COUNT)]


async def enqueue(
    redis: Redis,
    queue: QueueName,
    envelope: UpdateEnvelope,
) -> str:
    """Append an envelope to the user's queue partition and return the stream id."""

    key = stream_key(queue, partition_for_user(envelope.user_id))
    entry_id = await redis.xadd(
        key,
        cast("dict[Any, Any]", envelope.to_stream_entry()),
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )
    if isinstance(entry_id, bytes):
        return entry_id.decode("ascii")
    return str(entry_id)


async def ensure_groups(redis: Redis) -> None:
    """Create consumer groups for all queue partitions if they do not exist."""

    for queue_name, group_name in CONSUMER_GROUPS.items():
        for key in stream_keys(queue_name):
            try:
                await redis.xgroup_create(key, group_name, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise


async def read_group(
    redis: Redis,
    queue: QueueName,
    consumer: str,
    *,
    count: int = 1,
    block_ms: int | None = None,
) -> object:
    """Read pending work from all queue partitions for a consumer group."""

    return await redis.xreadgroup(
        CONSUMER_GROUPS[queue],
        consumer,
        streams={key: ">" for key in stream_keys(queue)},
        count=count,
        block=block_ms,
    )


async def ack(redis: Redis, queue: QueueName, stream: str, entry_id: str) -> int:
    """Acknowledge a processed stream entry."""

    acknowledged = await redis.xack(stream, CONSUMER_GROUPS[queue], entry_id)
    return int(acknowledged)


def decode_read_group_response(response: object, queue: QueueName) -> list[QueueMessage]:
    """Decode a Redis ``XREADGROUP`` response into queue messages."""

    messages: list[QueueMessage] = []
    for raw_stream, raw_entries in _iter_streams(response):
        stream = _to_text(raw_stream)
        for raw_entry_id, raw_fields in raw_entries:
            fields = cast("dict[str | bytes, str | bytes]", raw_fields)
            messages.append(
                QueueMessage(
                    queue=queue,
                    stream=stream,
                    entry_id=_to_text(raw_entry_id),
                    envelope=UpdateEnvelope.from_stream_entry(fields),
                )
            )
    return messages


async def delivery_count(
    redis: Redis,
    queue: QueueName,
    stream: str,
    entry_id: str,
) -> int:
    """Return the Redis consumer-group delivery count for a pending entry."""

    pending = await redis.xpending_range(
        stream,
        CONSUMER_GROUPS[queue],
        min=entry_id,
        max=entry_id,
        count=1,
    )
    if not pending:
        return 1
    return _pending_delivery_count(cast("object", pending[0]))


async def reclaim_stale_pending(
    redis: Redis,
    queue: QueueName,
    consumer: str,
    *,
    min_idle_ms: int = DEFAULT_RECLAIM_MIN_IDLE_MS,
    count: int = 10,
    start_id: str = "0-0",
) -> list[QueueMessage]:
    """Claim stale pending entries for a consumer and return them for processing."""

    claimed: list[QueueMessage] = []
    for stream in stream_keys(queue):
        response = await redis.xautoclaim(
            stream,
            CONSUMER_GROUPS[queue],
            consumer,
            min_idle_ms,
            start_id=start_id,
            count=count,
        )
        for entry_id, fields in _xautoclaim_entries(response):
            entry_id_text = _to_text(entry_id)
            message_delivery_count = await delivery_count(redis, queue, stream, entry_id_text)
            claimed.append(
                QueueMessage(
                    queue=queue,
                    stream=stream,
                    entry_id=entry_id_text,
                    envelope=UpdateEnvelope.from_stream_entry(
                        cast("dict[str | bytes, str | bytes]", fields)
                    ),
                    delivery_count=message_delivery_count,
                )
            )
    return claimed


async def move_to_dlq(
    redis: Redis,
    message: QueueMessage,
    *,
    delivery_count_value: int | None = None,
) -> str:
    """Move a message to the DLQ stream and acknowledge it on the source stream."""

    final_delivery_count = (
        message.delivery_count if delivery_count_value is None else delivery_count_value
    )
    envelope = message.envelope.model_copy(update={"attempt": final_delivery_count})
    dlq_entry = envelope.to_stream_entry()
    dlq_entry.update(
        {
            "source_stream": message.stream,
            "source_entry_id": message.entry_id,
            "delivery_count": str(final_delivery_count),
        }
    )
    dlq_id = await redis.xadd(DLQ_STREAM, cast("dict[Any, Any]", dlq_entry))
    await ack(redis, message.queue, message.stream, message.entry_id)
    return _to_text(dlq_id)


async def send_dlq_notifications(
    message: QueueMessage,
    *,
    notify_admin: NotifyAdminCallback,
    send_reply: SendReplyCallback,
) -> None:
    """Notify operators and the user that a message was moved to the DLQ."""

    await notify_admin(
        "DLQ message: "
        f"trace_id={message.envelope.trace_id} "
        f"update_id={message.envelope.update_id} "
        f"stream={message.stream} "
        f"entry_id={message.entry_id} "
        f"delivery_count={message.delivery_count}"
    )
    await send_reply(message.envelope.chat_id, USER_DLQ_MESSAGE)


def _iter_streams(response: object) -> list[tuple[object, list[tuple[object, object]]]]:
    if response is None:
        return []
    streams = cast("list[Any]", response)
    decoded: list[tuple[object, list[tuple[object, object]]]] = []
    for item in streams:
        raw_stream = cast("Any", item)[0]
        raw_entries = cast("list[tuple[object, object]]", cast("Any", item)[1])
        decoded.append((raw_stream, raw_entries))
    return decoded


def _xautoclaim_entries(response: object) -> list[tuple[object, object]]:
    raw = cast("Any", response)
    if not raw:
        return []
    entries = raw[1]
    return cast("list[tuple[object, object]]", entries)


def _pending_delivery_count(pending_entry: object) -> int:
    if isinstance(pending_entry, dict):
        for key in ("times_delivered", b"times_delivered"):
            raw_value = pending_entry.get(key)
            if raw_value is not None:
                return int(raw_value)
        return 1
    raw_sequence = cast("Any", pending_entry)
    return int(raw_sequence[3])


def _to_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii")
    return str(value)
