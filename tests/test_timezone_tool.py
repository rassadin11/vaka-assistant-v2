"""Unit tests for the set_timezone tool and shared timezone resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from core.context import TaskContext
from core.timezones import local_time_fields, resolve_timezone
from tools.timezone import (
    UNKNOWN_CITY_ERROR,
    SetTimezoneArgs,
    _set_timezone,
    register_timezone_tools,
)

USER_ID = UUID("018f0000-0000-7000-8000-000000000001")


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        if "set_config" in query:
            return "SELECT 1"
        if query.lstrip().startswith("UPDATE users"):
            self._pool.updates.append(str(args[0]))
            return "UPDATE 1"
        raise AssertionError(f"unexpected query: {query}")

    async def fetchval(self, query: str, *args: object) -> object:
        del args
        if "FROM scheduled_tasks" in query:
            return self._pool.recurring_reminders
        raise AssertionError(f"unexpected query: {query}")


class FakePool:
    def __init__(self, recurring_reminders: int = 0) -> None:
        self.recurring_reminders = recurring_reminders
        self.updates: list[str] = []
        self.connection = FakeConnection(self)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeRegistry:
    def __init__(self) -> None:
        self.specs: list[Any] = []

    def register(self, spec: Any) -> None:
        self.specs.append(spec)


def _context(timezone: str = "Europe/Moscow") -> TaskContext:
    return TaskContext(
        user_id=USER_ID,
        tg_user_id=101,
        chat_id=101,
        update_id=1,
        timezone=timezone,
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-0000000000ff"),
    )


@pytest.mark.parametrize(
    ("city", "expected"),
    [
        ("Новосибирск", "Asia/Novosibirsk"),
        ("  новосибирск  ", "Asia/Novosibirsk"),
        ("Нижний   Новгород", "Europe/Moscow"),
        ("Asia/Novosibirsk", "Asia/Novosibirsk"),
        ("Городок", None),
        ("Europe/Omsk", None),
        ("", None),
    ],
)
def test_resolve_timezone_accepts_cities_and_valid_identifiers_only(
    city: str,
    expected: str | None,
) -> None:
    assert resolve_timezone(city) == expected


def test_local_time_fields_render_in_the_requested_zone() -> None:
    moment = datetime(2026, 7, 20, 6, 30, tzinfo=UTC)

    assert local_time_fields("Europe/Moscow", moment) == {
        "local_time": "2026-07-20 09:30",
        "weekday": "monday",
    }
    assert local_time_fields("Asia/Kamchatka", moment)["local_time"] == "2026-07-20 18:30"


async def test_set_timezone_updates_the_user_and_reports_recurring_reminders() -> None:
    pool = FakePool(recurring_reminders=3)

    result = await _set_timezone(
        pool,  # type: ignore[arg-type]
        _context(),
        SetTimezoneArgs(city="Новосибирск"),
    )

    assert result.status == "ok"
    assert pool.updates == ["Asia/Novosibirsk"]
    assert result.payload["timezone"] == "Asia/Novosibirsk"
    assert result.payload["recurring_reminders"] == 3
    assert "unchanged" not in result.payload
    assert local_time_fields("Asia/Novosibirsk")["local_time"] == result.payload["local_time"]


async def test_set_timezone_keeps_the_database_untouched_when_zone_is_unchanged() -> None:
    pool = FakePool(recurring_reminders=1)

    result = await _set_timezone(
        pool,  # type: ignore[arg-type]
        _context("Europe/Moscow"),
        SetTimezoneArgs(city="Москва"),
    )

    assert result.status == "ok"
    assert result.payload["unchanged"] is True
    assert result.payload["recurring_reminders"] == 1
    assert pool.updates == []


async def test_set_timezone_rejects_an_unknown_city_retryably() -> None:
    pool = FakePool()

    result = await _set_timezone(
        pool,  # type: ignore[arg-type]
        _context(),
        SetTimezoneArgs(city="Городок"),
    )

    assert result.status == "error"
    assert result.error == UNKNOWN_CITY_ERROR
    assert result.retryable is True
    assert pool.updates == []


async def test_set_timezone_rejects_an_invented_identifier() -> None:
    pool = FakePool()

    result = await _set_timezone(
        pool,  # type: ignore[arg-type]
        _context(),
        SetTimezoneArgs(city="Europe/Omsk"),
    )

    assert result.status == "error"
    assert pool.updates == []


def test_set_timezone_is_registered_as_a_rate_limited_internal_mutation() -> None:
    registry = FakeRegistry()

    register_timezone_tools(registry, None)  # type: ignore[arg-type]

    (spec,) = registry.specs
    assert spec.name == "set_timezone"
    assert spec.risk == "mutating_internal"
    assert spec.daily_limit == 3
    assert set(spec.to_llm_definition().parameters["properties"]) == {"city"}
