"""Redis Streams queue helpers.

Streams are partitioned by Telegram user id. Changing ``PARTITION_COUNT`` after
data has been enqueued requires a deliberate rebalance because per-user order is
derived from this modulo.
"""

from __future__ import annotations

import os
import zlib
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
CONSUMER_GROUPS: dict[QueueName, str] = {
    "interactive": "g:interactive",
    "background": "g:background",
}


@dataclass(frozen=True, slots=True)
class RedisSettings:
    """Redis connection URLs loaded from environment variables."""

    queue_url: str
    cache_url: str


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
