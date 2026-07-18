"""Offline coverage for assistant persona tool handlers and registration."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from core.context import TaskContext
from core.tools import RiskLevel, ToolRegistry
from tests.test_tools import FakePool as RegistryFakePool
from tests.test_tools import FakeRedis
from tools.persona import (
    SetAssistantPersonaArgs,
    _clear_assistant_persona,
    _set_assistant_persona,
    register_persona_tools,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakePersonaConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakePersonaConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakePersonaConnection:
    def __init__(self, profile: dict[str, str] | None = None) -> None:
        self.profile = profile
        self.updates = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchval(self, query: str, *_args: object) -> str | None:
        assert "assistant_profile::text" in query
        return None if self.profile is None else json.dumps(self.profile, ensure_ascii=False)

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            return "SELECT 1"
        if "assistant_profile = NULL" in query:
            self.profile = None
        else:
            assert "assistant_profile = $1::jsonb" in query
            decoded: Any = json.loads(str(args[0]))
            assert isinstance(decoded, dict)
            self.profile = decoded
        self.updates += 1
        return "UPDATE 1"


class FakePersonaPool:
    def __init__(self, profile: dict[str, str] | None = None) -> None:
        self.connection = FakePersonaConnection(profile)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        update_id=101,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


async def test_set_persona_sanitizes_strings_and_preserves_omitted_fields() -> None:
    pool = FakePersonaPool({"address": "vy", "style": "Сдержанно"})

    result = await _set_assistant_persona(
        pool,
        _context(),
        SetAssistantPersonaArgs(name="  Джарвис\r\n Старший  "),
    )

    assert result.status == "ok"
    assert pool.connection.profile == {
        "name": "Джарвис Старший",
        "address": "vy",
        "style": "Сдержанно",
    }
    assert result.payload["assistant_profile"] == pool.connection.profile


async def test_set_persona_collapses_line_breaks_and_updates_only_sent_fields() -> None:
    pool = FakePersonaPool({"name": "Джарвис", "address": "vy", "style": "Сдержанно"})

    await _set_assistant_persona(
        pool,
        _context(),
        SetAssistantPersonaArgs(style="  Тепло \n\n и кратко\r\n ", address="ty"),
    )

    assert pool.connection.profile == {
        "name": "Джарвис",
        "address": "ty",
        "style": "Тепло и кратко",
    }


async def test_explicit_null_removes_only_that_persona_field() -> None:
    pool = FakePersonaPool({"name": "Джарвис", "address": "ty", "style": "Кратко"})

    await _set_assistant_persona(
        pool,
        _context(),
        SetAssistantPersonaArgs(name=None),
    )

    assert pool.connection.profile == {"address": "ty", "style": "Кратко"}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (SetAssistantPersonaArgs(name="я" * 31), "30 символов"),
        (SetAssistantPersonaArgs(style="я" * 201), "200 символов"),
    ],
)
async def test_set_persona_length_errors_are_clear_and_retryable(
    args: SetAssistantPersonaArgs,
    message: str,
) -> None:
    pool = FakePersonaPool()

    result = await _set_assistant_persona(pool, _context(), args)

    assert result.status == "error"
    assert result.retryable is True
    assert result.error is not None and message in result.error
    assert pool.connection.updates == 0


def test_set_persona_address_is_strict() -> None:
    assert SetAssistantPersonaArgs.model_validate({"address": "  ty\r\n"}).address == "ty"
    with pytest.raises(ValidationError):
        SetAssistantPersonaArgs.model_validate({"address": "them"})


async def test_clear_persona_sets_profile_to_null() -> None:
    pool = FakePersonaPool({"name": "Джарвис"})

    result = await _clear_assistant_persona(pool, _context())

    assert result.status == "ok"
    assert result.payload == {"cleared": True}
    assert pool.connection.profile is None


def test_persona_tools_are_core_mutations_with_set_daily_limit() -> None:
    redis = FakeRedis()
    pool = RegistryFakePool()
    registry = ToolRegistry(redis, pool)

    register_persona_tools(registry, pool)

    set_spec = registry.get("set_assistant_persona")
    clear_spec = registry.get("clear_assistant_persona")
    assert set_spec is not None
    assert set_spec.risk is RiskLevel.MUTATING_INTERNAL
    assert set_spec.daily_limit == 20
    assert clear_spec is not None
    assert clear_spec.risk is RiskLevel.MUTATING_INTERNAL
    assert clear_spec.daily_limit is None
