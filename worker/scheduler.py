"""Service-role poller that delivers due reminders."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import asyncpg
from asyncpg.pool import PoolConnectionProxy
from croniter import croniter

from core.db import service_transaction

SendReply = Callable[[int, str], Awaitable[None]]
Clock = Callable[[], datetime]
LOGGER = logging.getLogger(__name__)


class SchedulerProcessor:
    """Deliver due reminders inside one transaction per polling pass."""

    def __init__(
        self,
        service_pool: asyncpg.Pool,
        *,
        send_reply: SendReply,
        poll_seconds: float = 60.0,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service_pool = service_pool
        self._send_reply = send_reply
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
        """Send and finalize at most twenty currently due reminder rows."""

        async with service_transaction(self._service_pool) as connection:
            rows = await connection.fetch(
                """
                SELECT s.*, u.tg_chat_id, u.timezone
                FROM scheduled_tasks AS s
                JOIN users AS u ON u.id = s.user_id
                WHERE s.kind = 'reminder'
                  AND s.status = 'active'
                  AND s.next_run_at <= now()
                ORDER BY s.next_run_at
                FOR UPDATE OF s SKIP LOCKED
                LIMIT 20
                """
            )
            for row in rows:
                await self._send_reply(int(row["tg_chat_id"]), f"Напоминание: {row['payload']}")
                await self._finalize(connection, row)
        return bool(rows)

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
