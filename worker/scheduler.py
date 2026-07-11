"""Service-role poller for due reminders and recurring agent tasks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
from asyncpg.pool import PoolConnectionProxy
from croniter import croniter

from core.db import service_transaction
from core.envelope import UpdateEnvelope
from core.tracing import reset_trace_id, set_trace_id

SendReply = Callable[[int, str], Awaitable[None]]
EnqueueBackground = Callable[[UpdateEnvelope], Awaitable[object]]
Clock = Callable[[], datetime]
LOGGER = logging.getLogger(__name__)


class SchedulerProcessor:
    """Deliver due reminders and enqueue due agent tasks in one transaction."""

    def __init__(
        self,
        service_pool: asyncpg.Pool,
        *,
        send_reply: SendReply,
        enqueue_background: EnqueueBackground | None = None,
        poll_seconds: float = 60.0,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service_pool = service_pool
        self._send_reply = send_reply
        self._enqueue_background = enqueue_background
        self._poll_seconds = poll_seconds
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._logger = logger if logger is not None else LOGGER
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Request that the polling loop stops after the current pass."""

        self._stop.set()

    async def run(self) -> None:
        """Run until shutdown, isolating failures to an individual polling pass."""

        while not self._stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                self._logger.exception("scheduler polling failed")
                worked = False
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)

    async def run_once(self) -> bool:
        """Process at most twenty currently due scheduled task rows."""

        async with service_transaction(self._service_pool) as connection:
            rows = await connection.fetch(
                """
                SELECT s.*, u.tg_user_id, u.tg_chat_id, u.timezone
                FROM scheduled_tasks AS s
                JOIN users AS u ON u.id = s.user_id
                WHERE s.kind IN ('reminder', 'agent_task')
                  AND s.status = 'active'
                  AND s.next_run_at <= now()
                ORDER BY s.next_run_at
                FOR UPDATE OF s SKIP LOCKED
                LIMIT 20
                """
            )
            for row in rows:
                trace_id = uuid4()
                token = set_trace_id(str(trace_id))
                try:
                    if row["kind"] == "reminder":
                        await self._send_reply(
                            int(row["tg_chat_id"]), f"Напоминание: {row['payload']}"
                        )
                    elif row["kind"] == "agent_task":
                        await self._enqueue_agent_task(row, trace_id=trace_id)
                    else:
                        raise ValueError(f"unsupported scheduled task kind: {row['kind']}")
                    await self._finalize(connection, row)
                finally:
                    reset_trace_id(token)
        return bool(rows)

    async def _enqueue_agent_task(self, row: asyncpg.Record, *, trace_id: UUID) -> None:
        if self._enqueue_background is None:
            raise RuntimeError("scheduler agent-task enqueue callback is not configured")
        next_run_at = row["next_run_at"]
        if not isinstance(next_run_at, datetime):
            raise TypeError("scheduled agent task next_run_at is not a datetime")
        await self._enqueue_background(
            UpdateEnvelope(
                update_id=agent_task_update_id(int(row["id"]), next_run_at),
                user_id=int(row["tg_user_id"]),
                chat_id=int(row["tg_chat_id"]),
                kind="agent_task",
                payload={
                    "text": str(row["payload"]),
                    "scheduled_task_id": int(row["id"]),
                    "title": str(row["title"]),
                },
                trace_id=trace_id,
            )
        )

    async def _finalize(
        self,
        connection: PoolConnectionProxy[asyncpg.Record],
        row: asyncpg.Record,
    ) -> None:
        cron_expr = row["cron_expr"]
        if cron_expr is None:
            await connection.execute(
                """
                UPDATE scheduled_tasks
                SET status = 'done', last_run_at = now()
                WHERE id = $1
                """,
                row["id"],
            )
            return
        if not isinstance(cron_expr, str):
            raise TypeError("scheduled reminder cron_expr is not a string")
        timezone = ZoneInfo(str(row["timezone"]))
        current = self._clock().astimezone(timezone)
        next_run_at = croniter(cron_expr, current).get_next(datetime).astimezone(UTC)
        await connection.execute(
            """
            UPDATE scheduled_tasks
            SET next_run_at = $2, last_run_at = now()
            WHERE id = $1
            """,
            row["id"],
            next_run_at,
        )


def agent_task_update_id(task_id: int, next_run_at: datetime) -> int:
    """Derive a stable synthetic update id for one scheduled task firing."""

    digest = hashlib.sha256(f"{task_id}:{int(next_run_at.timestamp())}".encode()).digest()
    return 10**12 + int.from_bytes(digest[:8], "big") % 10**12
