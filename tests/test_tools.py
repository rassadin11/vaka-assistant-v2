"""Offline contract coverage for the stage-4 registry and dispatcher."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.llm import LLMToolCall
from core.registry_dispatcher import RegistryToolDispatcher
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec
from core.tools_dispatch import MalformedToolCallError
from tools.registry import register_builtin_tools


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
    def __init__(self) -> None:
        self.logs: list[tuple[object, ...]] = []
        self.outbox: list[dict[str, object]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            return "SELECT 1"
        if "INSERT INTO outbox_actions" in query:
            self.outbox.append(
                {"id": args[0], "user_id": args[1], "action": args[2], "status": "pending"}
            )
            return "INSERT 0 1"
        if "INSERT INTO tool_calls_log" not in query:
            raise AssertionError(f"unexpected query: {query}")
        self.logs.append(args)
        return "INSERT 0 1"


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
        keepttl: bool = False,
    ) -> object:
        if nx and name in self.values:
            return False
        if xx and name not in self.values:
            return False
        self.values[name] = value
        if ex is not None:
            self.expiries[name] = ex
        if not keepttl and ex is None:
            self.expiries.pop(name, None)
        return True

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def incr(self, name: str) -> int:
        value = int(self.values.get(name, "0")) + 1
        self.values[name] = str(value)
        return value

    async def expire(self, name: str, time: int) -> bool:
        if name not in self.values:
            return False
        self.expiries[name] = time
        return True

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
        return deleted


class ValueArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: int = Field(ge=1)


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


def _registry(
    *,
    sender: Any | None = None,
) -> tuple[ToolRegistry, FakeRedis, FakePool]:
    redis = FakeRedis()
    pool = FakePool()
    return ToolRegistry(redis, pool, send_confirmation=sender), redis, pool


def _spec(
    name: str,
    risk: RiskLevel,
    handler: Any,
    *,
    daily_limit: int | None = None,
) -> ToolSpec:
    return ToolSpec(name, "A test tool.", ValueArgs, risk, handler, daily_limit)


async def test_user_id_is_never_passed_from_model_arguments() -> None:
    registry, _redis, _pool = _registry()
    seen: list[int] = []

    async def handler(_ctx: TaskContext, args: BaseModel) -> ToolResult:
        seen.append(args.model_dump()["value"])
        return ToolResult(status="ok")

    registry.register(_spec("safe", RiskLevel.READ_ONLY, handler))
    result = await registry.dispatch(_context(), "safe", {"value": 2, "user_id": "attacker"}, 1)

    assert result.status == "ok"
    assert seen == [2]


async def test_validation_retries_cap_and_malformed_calls_remain_distinct() -> None:
    registry, _redis, _pool = _registry()

    async def handler(_ctx: TaskContext, _args: BaseModel) -> ToolResult:
        return ToolResult(status="ok")

    registry.register(_spec("safe", RiskLevel.READ_ONLY, handler))
    dispatcher = RegistryToolDispatcher(registry)
    context = _context()
    for expected_retryable in (True, True, False):
        raw = await dispatcher.dispatch(
            LLMToolCall(id="1", name="safe", arguments_json="{}"), context
        )
        assert ToolResult.model_validate_json(raw).retryable is expected_retryable

    with pytest.raises(MalformedToolCallError):
        await dispatcher.dispatch(LLMToolCall(id="2", name="missing", arguments_json="{}"), context)
    with pytest.raises(MalformedToolCallError):
        await dispatcher.dispatch(LLMToolCall(id="3", name="safe", arguments_json="{"), context)


async def test_external_tool_creates_pending_key_and_confirmation_buttons() -> None:
    sent: list[tuple[int, str, list[list[tuple[str, str]]]]] = []

    async def sender(chat_id: int, text: str, buttons: list[list[tuple[str, str]]]) -> None:
        sent.append((chat_id, text, buttons))

    registry, redis, pool = _registry(sender=sender)
    calls = 0

    async def handler(_ctx: TaskContext, _args: BaseModel) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(status="ok")

    registry.register(_spec("external", RiskLevel.MUTATING_EXTERNAL, handler))
    result = await registry.dispatch(_context(), "external", {"value": 1}, 1)

    confirmation_id = result.payload["confirmation_id"]
    key = f"pending:{_context().user_id}:{confirmation_id}"
    assert result.status == "pending_confirmation"
    assert calls == 0
    assert redis.expiries[key] == 900
    assert json.loads(redis.values[key])["tool_name"] == "external"
    assert sent == [
        (
            500,
            "Подтвердите действие: external.",
            [
                [
                    ("Выполнить", f"confirm:{confirmation_id}"),
                    ("Отменить", f"cancel:{confirmation_id}"),
                ]
            ],
        )
    ]
    assert [args[4] for args in pool.connection.logs] == ["pending_confirmation"]


async def test_daily_limit_idempotency_and_all_result_statuses_are_audited() -> None:
    registry, redis, pool = _registry(sender=_sender)
    calls = 0

    async def handler(_ctx: TaskContext, _args: BaseModel) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(status="ok", payload={"text": "x" * 600})

    registry.register(_spec("limited", RiskLevel.MUTATING_INTERNAL, handler, daily_limit=1))
    registry.register(_spec("external", RiskLevel.MUTATING_EXTERNAL, handler))
    invalid = await registry.dispatch(_context(), "limited", {}, 1)
    first = await registry.dispatch(_context(), "limited", {"value": 1}, 2)
    repeated = await registry.dispatch(_context(), "limited", {"value": 1}, 2)
    limited = await registry.dispatch(_context(), "limited", {"value": 2}, 3)
    pending = await registry.dispatch(_context(), "external", {"value": 1}, 4)

    assert invalid.retryable is True
    assert first.status == repeated.status == "ok"
    assert calls == 1
    assert limited.retryable is False
    assert pending.status == "pending_confirmation"
    assert {args[4] for args in pool.connection.logs} == {"ok", "error", "pending_confirmation"}
    assert redis.values["idem:101:2"] == first.model_dump_json()


async def _sender(_chat_id: int, _text: str, _buttons: list[list[tuple[str, str]]]) -> None:
    return None


def test_registered_schemas_follow_the_v1_contract() -> None:
    registry, redis, pool = _registry(sender=_sender)
    register_builtin_tools(registry, pool, cache_redis=redis)

    set_persona = registry.get("set_assistant_persona")
    clear_persona = registry.get("clear_assistant_persona")
    assert set_persona is not None and set_persona.daily_limit == 20
    assert clear_persona is not None

    for spec in registry.get_for_context(_context()):
        schema = spec.to_llm_definition().parameters
        properties = schema.get("properties", {})
        assert len(properties) <= 5
        assert "user_id" not in properties
        assert "$defs" not in schema
        assert all(not _has_nested_object(value, 0) for value in properties.values())


def _has_nested_object(value: object, depth: int) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("type") == "object" and depth > 1:
        return True
    return any(_has_nested_object(item, depth + 1) for item in value.values())
