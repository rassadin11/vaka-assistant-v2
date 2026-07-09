"""Async Redis Streams worker skeleton."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from core.envelope import UpdateEnvelope
from core.locks import DEFAULT_USER_LOCK_TTL_MS, acquire_user_lock, release_user_lock
from core.queue import (
    DEFAULT_MAX_DELIVERIES,
    DEFAULT_RECLAIM_MIN_IDLE_MS,
    QueueMessage,
    QueueName,
    ack,
    decode_read_group_response,
    delivery_count,
    ensure_groups,
    has_pending_predecessor,
    move_to_dlq,
    read_group,
    reclaim_stale_pending,
    send_dlq_notifications,
)
from worker.processor import Processor

SendReplyCallback = Callable[[int, str], Awaitable[None]]
SendTypingCallback = Callable[[int], Awaitable[None]]
NotifyAdminCallback = Callable[[str], Awaitable[None]]

LOGGER = logging.getLogger(__name__)
WORKER_DEDUP_TTL_SECONDS = 86_400


class CacheRedis(Protocol):
    """Subset of Redis cache commands used by the worker."""

    def exists(self, name: str) -> Awaitable[int]: ...

    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Runtime configuration for one worker instance."""

    consumer_name: str = field(default_factory=lambda: f"worker-{uuid4().hex[:12]}")
    worker_token: str = field(default_factory=lambda: uuid4().hex)
    interactive_only: bool = False
    interactive_block_ms: int = 2_000
    background_block_ms: int = 2_000
    lock_ttl_ms: int = DEFAULT_USER_LOCK_TTL_MS
    reclaim_interval_seconds: float = 30.0
    reclaim_min_idle_ms: int = DEFAULT_RECLAIM_MIN_IDLE_MS
    max_deliveries: int = DEFAULT_MAX_DELIVERIES
    typing_interval_seconds: float = 5.0
    lock_retry_sleep_seconds: float = 0.25
    lock_wait_timeout_seconds: float = 5.0
    stream_order_retry_sleep_seconds: float = 0.05
    stream_order_wait_timeout_seconds: float = 2.0
    reconnect_backoff_initial_seconds: float = 0.5
    reconnect_backoff_max_seconds: float = 10.0
    process_timeout_seconds: float = 120.0


class Worker:
    """Consume queued updates and hand them to a processor."""

    def __init__(
        self,
        *,
        queue_redis: Redis,
        cache_redis: CacheRedis,
        processor: Processor,
        send_reply: SendReplyCallback,
        send_typing: SendTypingCallback,
        notify_admin: NotifyAdminCallback,
        config: WorkerConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._queue_redis = queue_redis
        self._cache_redis = cache_redis
        self._processor = processor
        self._send_reply = send_reply
        self._send_typing = send_typing
        self._notify_admin = notify_admin
        self._config = config if config is not None else WorkerConfig()
        self._logger = logger if logger is not None else LOGGER
        self._stop_event = asyncio.Event()
        self._next_reclaim_at = time.monotonic() + self._config.reclaim_interval_seconds

    def request_stop(self) -> None:
        """Request a graceful stop after the current message finishes."""

        self._stop_event.set()

    async def run(self) -> None:
        """Run the worker until a graceful stop is requested."""

        await ensure_groups(self._queue_redis)
        backoff = self._config.reconnect_backoff_initial_seconds
        while not self._stop_event.is_set():
            try:
                await self.run_once()
                backoff = self._config.reconnect_backoff_initial_seconds
            except RedisConnectionError as exc:
                self._logger.warning("redis connection failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._config.reconnect_backoff_max_seconds)

    async def run_once(self) -> bool:
        """Run one reclaim or consume iteration. Return whether a message was handled."""

        reclaimed = await self._reclaim_if_due()
        if reclaimed:
            return True

        messages = await self._read_next_messages()
        if not messages:
            return False
        # XREADGROUP returns up to `count` entries PER stream, so one read over
        # 16 partitions can deliver several messages; every delivered entry must
        # be handled, or it silently waits for the 210s reclaim and its delivery
        # counter drifts toward the DLQ.
        for message in messages:
            await self._handle_message(message)
        return True

    async def _reclaim_if_due(self) -> bool:
        now = time.monotonic()
        if now < self._next_reclaim_at:
            return False
        self._next_reclaim_at = now + self._config.reclaim_interval_seconds
        queues: tuple[QueueName, ...] = (
            ("interactive",) if self._config.interactive_only else ("interactive", "background")
        )
        handled = False
        for queue in queues:
            messages = await reclaim_stale_pending(
                self._queue_redis,
                queue,
                self._config.consumer_name,
                min_idle_ms=self._config.reclaim_min_idle_ms,
            )
            # XAUTOCLAIM has already bumped the delivery counter for every
            # claimed entry, so each one must be handled now — a dropped entry
            # would drift toward the DLQ without a single processing attempt.
            for message in messages:
                if self._stop_event.is_set():
                    return handled
                await self._handle_message(message)
                handled = True
        return handled

    async def _read_next_messages(self) -> list[QueueMessage]:
        if self._config.interactive_only:
            return await self._read_queue("interactive", block_ms=self._config.interactive_block_ms)

        interactive = await self._read_queue("interactive", block_ms=None)
        if interactive:
            return interactive
        return await self._read_queue("background", block_ms=self._config.background_block_ms)

    async def _read_queue(self, queue: QueueName, *, block_ms: int | None) -> list[QueueMessage]:
        response = await read_group(
            self._queue_redis,
            queue,
            self._config.consumer_name,
            count=1,
            block_ms=block_ms,
        )
        return decode_read_group_response(response, queue)

    async def _handle_message(self, message: QueueMessage) -> None:
        envelope = message.envelope
        log_extra = {"trace_id": str(envelope.trace_id)}
        self._logger.info(
            "message received queue=%s stream=%s entry_id=%s",
            message.queue,
            message.stream,
            message.entry_id,
            extra=log_extra,
        )

        if not await self._wait_for_stream_predecessors(message):
            return

        lock_wait_deadline = time.monotonic() + self._config.lock_wait_timeout_seconds
        while not self._stop_event.is_set():
            locked = await acquire_user_lock(
                self._queue_redis,
                envelope.user_id,
                self._config.worker_token,
                ttl_ms=self._config.lock_ttl_ms,
            )
            if locked:
                break
            self._logger.info("user lock busy", extra=log_extra)
            if time.monotonic() >= lock_wait_deadline:
                return
            await asyncio.sleep(self._config.lock_retry_sleep_seconds)
        else:
            return

        try:
            current_delivery_count = await self._message_delivery_count(message)
            envelope = envelope.model_copy(update={"attempt": current_delivery_count})
            message = QueueMessage(
                queue=message.queue,
                stream=message.stream,
                entry_id=message.entry_id,
                envelope=envelope,
                delivery_count=current_delivery_count,
            )

            if current_delivery_count >= self._config.max_deliveries:
                await move_to_dlq(self._queue_redis, message)
                await send_dlq_notifications(
                    message,
                    notify_admin=self._notify_admin,
                    send_reply=self._send_reply,
                )
                self._logger.warning("message moved to dlq", extra=log_extra)
                return

            dedup_key = f"dedup:worker:{envelope.update_id}"
            if await self._cache_redis.exists(dedup_key):
                await ack(self._queue_redis, message.queue, message.stream, message.entry_id)
                self._logger.info("duplicate message skipped", extra=log_extra)
                return

            reply_text = await self._process_with_typing(envelope)
            if reply_text is not None:
                await self._send_reply(envelope.chat_id, reply_text)
            await self._cache_redis.set(dedup_key, "1", ex=WORKER_DEDUP_TTL_SECONDS, nx=True)
            await ack(self._queue_redis, message.queue, message.stream, message.entry_id)
            self._logger.info("message acknowledged", extra=log_extra)
        except TimeoutError:
            self._logger.exception("message processing timed out", extra=log_extra)
        except RedisConnectionError:
            self._logger.exception(
                "redis connection failed while handling message",
                extra=log_extra,
            )
            raise
        except Exception:
            self._logger.exception("message processing failed", extra=log_extra)
        finally:
            await release_user_lock(
                self._queue_redis,
                envelope.user_id,
                self._config.worker_token,
            )

    async def _message_delivery_count(self, message: QueueMessage) -> int:
        if message.delivery_count > 1:
            return message.delivery_count
        return await delivery_count(
            self._queue_redis,
            message.queue,
            message.stream,
            message.entry_id,
        )

    async def _wait_for_stream_predecessors(self, message: QueueMessage) -> bool:
        deadline = time.monotonic() + self._config.stream_order_wait_timeout_seconds
        while not self._stop_event.is_set():
            if not await has_pending_predecessor(
                self._queue_redis,
                message.queue,
                message.stream,
                message.entry_id,
                user_id=message.envelope.user_id,
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self._config.stream_order_retry_sleep_seconds)
        return False

    async def _process_with_typing(self, envelope: UpdateEnvelope) -> str | None:
        await self._send_typing(envelope.chat_id)
        typing_task = asyncio.create_task(self._typing_loop(envelope.chat_id))
        try:
            return await asyncio.wait_for(
                self._processor.process(envelope),
                timeout=self._config.process_timeout_seconds,
            )
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

    async def _typing_loop(self, chat_id: int) -> None:
        while True:
            await asyncio.sleep(self._config.typing_interval_seconds)
            await self._send_typing(chat_id)
