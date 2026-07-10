"""Worker-side handling of mutating-external confirmation callbacks."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

import asyncpg
import uuid_utils

from core.context import TaskContext
from core.db import user_transaction
from core.envelope import UpdateEnvelope
from core.tools import QueueRedis

SendReply = Callable[[int, str], Awaitable[None]]
LOGGER = logging.getLogger(__name__)


class ConfirmationProcessor:
    """Turn a trusted Telegram callback into a durable outbox action."""

    def __init__(
        self,
        queue_redis: QueueRedis,
        app_pool: asyncpg.Pool,
        *,
        send_reply: SendReply,
    ) -> None:
        self._queue_redis = queue_redis
        self._app_pool = app_pool
        self._send_reply = send_reply

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        """Consume confirm/cancel callbacks; unrelated callbacks are not handled."""

        data = envelope.payload.get("data")
        if not isinstance(data, str):
            return None
        action, separator, confirmation_id = data.partition(":")
        if separator != ":" or action not in {"confirm", "cancel"} or not confirmation_id:
            return None
        key = f"pending:{context.user_id}:{confirmation_id}"
        raw = await self._queue_redis.get(key)
        if raw is None:
            return "Действие устарело."
        try:
            pending = _pending_payload(raw)
        except ValueError:
            await self._queue_redis.delete(key)
            return "Действие устарело."

        if action == "cancel":
            await self._queue_redis.delete(key)
            return "Действие отменено."

        await self._insert_outbox(context, pending)
        await self._queue_redis.delete(key)
        return "Действие подтверждено и поставлено в очередь."

    async def _insert_outbox(self, context: TaskContext, action: Mapping[str, object]) -> None:
        async with user_transaction(self._app_pool, context.user_id) as connection:
            await connection.execute(
                """
                INSERT INTO outbox_actions (
                    id, user_id, action, status, attempts, created_at
                )
                VALUES ($1, $2, $3::jsonb, 'pending', 0, now())
                """,
                UUID(str(uuid_utils.uuid7())),
                context.user_id,
                json.dumps(action, ensure_ascii=False, separators=(",", ":")),
            )


def _pending_payload(value: str | bytes) -> dict[str, object]:
    raw = value.decode("utf-8") if isinstance(value, bytes) else value
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("pending value is not an object")
    required = {"tool_name", "args", "update_id", "chat_id", "trace_id"}
    if not required.issubset(parsed):
        raise ValueError("pending payload is incomplete")
    return parsed
