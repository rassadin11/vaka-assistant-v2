"""Offline confirmation callback coverage."""

from __future__ import annotations

import json
from uuid import UUID

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from worker.confirmations import ConfirmationProcessor


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.outbox: list[dict[str, object]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            return "SELECT 1"
        if "INSERT INTO outbox_actions" in query:
            self.outbox.append(
                {"id": args[0], "user_id": args[1], "action": args[2], "status": "pending"}
            )
            return "INSERT 0 1"
        raise AssertionError(f"unexpected query: {query}")


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Connection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.connection = _Connection()

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            if name in self.values:
                count += 1
                del self.values[name]
        return count


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        update_id=1,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


def _envelope(data: str) -> UpdateEnvelope:
    return UpdateEnvelope(
        update_id=2,
        user_id=100,
        chat_id=500,
        kind="callback",
        payload={"data": data, "callback_query_id": "callback"},
    )


async def _send(_chat_id: int, _text: str) -> None:
    return None


async def test_confirm_moves_pending_payload_to_outbox_and_cancel_removes_it() -> None:
    redis = FakeRedis()
    pool = FakePool()
    processor = ConfirmationProcessor(redis, pool, send_reply=_send)  # type: ignore[arg-type]
    confirmation_id = "abc"
    key = f"pending:{_context().user_id}:{confirmation_id}"
    redis.values[key] = json.dumps(
        {
            "tool_name": "external",
            "args": {"value": 1},
            "update_id": 1,
            "chat_id": 500,
            "trace_id": str(_context().trace_id),
        }
    )

    result = await processor.process(_envelope(f"confirm:{confirmation_id}"), _context())

    assert result == "Действие подтверждено и поставлено в очередь."
    assert key not in redis.values
    assert len(pool.connection.outbox) == 1
    assert pool.connection.outbox[0]["status"] == "pending"

    cancel_key = f"pending:{_context().user_id}:cancel"
    redis.values[cancel_key] = json.dumps(
        {
            "tool_name": "external",
            "args": {"value": 1},
            "update_id": 1,
            "chat_id": 500,
            "trace_id": str(_context().trace_id),
        }
    )
    assert await processor.process(_envelope("cancel:cancel"), _context()) == "Действие отменено."
    assert cancel_key not in redis.values


async def test_stale_confirmation_reports_expiration() -> None:
    processor = ConfirmationProcessor(FakeRedis(), FakePool(), send_reply=_send)  # type: ignore[arg-type]

    assert await processor.process(_envelope("confirm:missing"), _context()) == "Действие устарело."
