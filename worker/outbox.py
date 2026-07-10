"""Service-role processor for durable confirmed external actions."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from uuid import UUID

import asyncpg

from core.context import TaskContext
from core.db import service_transaction
from core.tools import ToolRegistry

SendReply = Callable[[int, str], Awaitable[None]]
LOGGER = logging.getLogger(__name__)


class OutboxProcessor:
    """Poll, claim, execute, and finalize confirmed actions as the service role."""

    def __init__(
        self,
        service_pool: asyncpg.Pool,
        registry: ToolRegistry,
        *,
        send_reply: SendReply,
        poll_seconds: float = 1.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service_pool = service_pool
        self._registry = registry
        self._send_reply = send_reply
        self._poll_seconds = poll_seconds
        self._logger = logger if logger is not None else LOGGER
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Request that the polling loop exits after the current action."""

        self._stop.set()

    async def run(self) -> None:
        """Run until shutdown, with failures isolated to individual polling passes."""

        while not self._stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                self._logger.exception("outbox polling failed")
                worked = False
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)

    async def run_once(self) -> bool:
        """Claim and process at most one pending action."""

        row = await self._claim()
        if row is None:
            return False
        action_id = row["id"]
        action = _object(row["action"])
        try:
            context = await self._context_for(row["user_id"], action)
            result = await self._registry.execute_outbox(
                context,
                _string(action, "tool_name"),
                _mapping(action, "args"),
            )
            if result.status == "ok":
                await self._finish(action_id, "done", None)
                await self._send_reply(context.chat_id, "Подтверждённое действие выполнено.")
            else:
                await self._finish(action_id, "failed", result.error)
                await self._send_reply(
                    context.chat_id,
                    "Не удалось выполнить подтверждённое действие: "
                    f"{result.error or 'неизвестная ошибка'}",
                )
        except Exception as exc:
            self._logger.exception("outbox action failed", extra={"action_id": str(action_id)})
            await self._retry_or_fail(action_id, str(exc))
        return True

    async def _claim(self) -> asyncpg.Record | None:
        async with service_transaction(self._service_pool) as connection:
            return await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT id
                    FROM outbox_actions
                    WHERE status = 'pending'
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE outbox_actions AS action
                SET status = 'executing', attempts = attempts + 1, executed_at = now()
                FROM candidate
                WHERE action.id = candidate.id
                RETURNING action.id, action.user_id, action.action, action.attempts
                """
            )

    async def _context_for(self, user_id: UUID, action: Mapping[str, object]) -> TaskContext:
        async with service_transaction(self._service_pool) as connection:
            row = await connection.fetchrow(
                """
                SELECT id, tg_user_id, tg_chat_id, timezone, plan
                FROM users
                WHERE id = $1
                """,
                user_id,
            )
        if row is None:
            raise ValueError("outbox user no longer exists")
        return TaskContext(
            user_id=row["id"],
            tg_user_id=row["tg_user_id"],
            chat_id=row["tg_chat_id"],
            update_id=_int(action, "update_id"),
            timezone=row["timezone"],
            plan=row["plan"],
            trace_id=UUID(_string(action, "trace_id")),
        )

    async def _finish(self, action_id: UUID, status: str, error: str | None) -> None:
        async with service_transaction(self._service_pool) as connection:
            await connection.execute(
                "UPDATE outbox_actions "
                "SET status = $2, last_error = $3, executed_at = now() WHERE id = $1",
                action_id,
                status,
                error,
            )

    async def _retry_or_fail(self, action_id: UUID, error: str) -> None:
        async with service_transaction(self._service_pool) as connection:
            row = await connection.fetchrow(
                """
                UPDATE outbox_actions
                SET status = CASE WHEN attempts >= 3 THEN 'failed' ELSE 'pending' END,
                    last_error = $2
                WHERE id = $1
                RETURNING attempts
                """,
                action_id,
                error[:500],
            )
        if row is None:
            self._logger.warning("outbox action disappeared action_id=%s", action_id)


def _object(value: object) -> dict[str, object]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("outbox action is not an object")
    return parsed


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"outbox action field {key} is not an object")
    return item


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"outbox action field {key} is not a string")
    return item


def _int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int):
        raise ValueError(f"outbox action field {key} is not an integer")
    return item
