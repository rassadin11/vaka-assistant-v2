"""Unit tests for non-LLM usage persistence shapes."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from core.usage_store import save_stt_usage


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        self.calls.append((query, args))
        return "OK"


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


async def test_save_stt_usage_uses_nullable_tokens_and_interactive_groq_row() -> None:
    pool = FakePool()
    user_id = UUID("018f0000-0000-7000-8000-000000000001")
    trace_id = UUID("018f0000-0000-7000-8000-000000000002")

    await save_stt_usage(pool, user_id, trace_id, Decimal("0.000123"))  # type: ignore[arg-type]

    query, arguments = pool.connection.calls[-1]
    assert "'stt:groq'" in query
    assert "NULL, NULL, NULL" in query
    assert "'interactive'" in query
    assert arguments == (user_id, trace_id, Decimal("0.000123"))
