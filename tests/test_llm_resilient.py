"""Unit tests for Redis-backed LLM resilience controls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from core.llm import LLMMessage, LLMProviderError, LLMRateLimitError, LLMServerError
from core.llm_mock import MockLLMProvider, mock_text_response
from core.llm_resilient import (
    SEMAPHORE_ACQUIRE_LUA,
    SEMAPHORE_KEY,
    SEMAPHORE_RELEASE_LUA,
    ResilientLLMConfig,
    ResilientLLMProvider,
)


class FakeClock:
    """Controllable monotonic clock shared with the fake Redis expiry state."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeRedis:
    """Small in-memory subset of Redis used by the resilient provider."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.values: dict[str, str] = {}
        self.expiries: dict[str, float] = {}
        self.eval_calls: list[str] = []

    async def eval(self, script: str, numkeys: int, *args: object) -> int:
        assert numkeys == 1
        self.eval_calls.append(script)
        key = str(args[0])
        self._expire_due_keys()
        if script == SEMAPHORE_ACQUIRE_LUA:
            limit = int(args[1])
            ttl = int(args[2])
            if int(self.values.get(key, "0")) >= limit:
                return 0
            self.values[key] = str(int(self.values.get(key, "0")) + 1)
            self.expiries[key] = self._clock() + ttl
            return 1
        if script == SEMAPHORE_RELEASE_LUA:
            remaining = max(0, int(self.values.get(key, "0")) - 1)
            if key in self.values:
                self.values[key] = str(remaining)
            return remaining
        raise AssertionError(f"unexpected Lua script: {script}")

    async def get(self, name: str) -> str | None:
        self._expire_due_keys()
        return self.values.get(name)

    async def incr(self, name: str) -> int:
        self._expire_due_keys()
        value = int(self.values.get(name, "0")) + 1
        self.values[name] = str(value)
        return value

    async def expire(self, name: str, time: int) -> bool:
        self._expire_due_keys()
        if name not in self.values:
            return False
        self.expiries[name] = self._clock() + time
        return True

    async def delete(self, *names: str) -> int:
        self._expire_due_keys()
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
            self.expiries.pop(name, None)
        return deleted

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        self._expire_due_keys()
        if nx and name in self.values:
            return False
        self.values[name] = value
        if ex is not None:
            self.expiries[name] = self._clock() + ex
        return True

    def _expire_due_keys(self) -> None:
        for name, expiry in list(self.expiries.items()):
            if expiry <= self._clock():
                self.values.pop(name, None)
                del self.expiries[name]


def _provider(
    primary: MockLLMProvider,
    redis: FakeRedis,
    *,
    fallback: MockLLMProvider | None = None,
    clock: FakeClock | None = None,
    config: ResilientLLMConfig | None = None,
    notify_admin: Callable[[str], Awaitable[None]] | None = None,
    rng: Callable[[float, float], float] | None = None,
) -> ResilientLLMProvider:
    return ResilientLLMProvider(
        primary,
        redis,
        "primary/model",
        fallback_provider=fallback,
        config=config,
        notify_admin=notify_admin,
        clock=clock,
        sleep=clock.sleep if clock is not None else None,
        rng=rng,
    )


async def test_semaphore_acquires_releases_and_releases_after_provider_error() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    primary = MockLLMProvider.scripted([LLMProviderError("broken")])
    provider = _provider(primary, redis, clock=clock)

    with pytest.raises(LLMProviderError, match="broken"):
        await provider.generate([LLMMessage(role="user", content="hello")])

    assert await redis.get(SEMAPHORE_KEY) == "0"
    assert redis.eval_calls == [SEMAPHORE_ACQUIRE_LUA, SEMAPHORE_RELEASE_LUA]


async def test_busy_semaphore_polls_until_timeout() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    redis.values[SEMAPHORE_KEY] = "1"
    config = ResilientLLMConfig(max_concurrency=1, semaphore_wait_seconds=0.5)
    provider = _provider(MockLLMProvider.scripted([]), redis, clock=clock, config=config)

    with pytest.raises(LLMProviderError, match="concurrency limit"):
        await provider.generate([LLMMessage(role="user", content="hello")])

    assert clock.now == pytest.approx(0.5)
    assert len(redis.eval_calls) == 4


async def test_retries_retryable_errors_with_full_jitter() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    primary = MockLLMProvider.scripted(
        [LLMRateLimitError("429"), LLMServerError("503"), mock_text_response("done")]
    )
    delays: list[tuple[float, float]] = []

    def rng(low: float, high: float) -> float:
        delays.append((low, high))
        return high / 2

    provider = _provider(primary, redis, clock=clock, rng=rng)

    response = await provider.generate([LLMMessage(role="user", content="hello")])

    assert response.message.content == "done"
    assert len(primary.calls) == 3
    assert delays == [(0, 1), (0, 2)]
    assert clock.now == pytest.approx(1.5)
    assert await redis.get(SEMAPHORE_KEY) == "0"


async def test_non_retryable_error_propagates_immediately() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    primary = MockLLMProvider.scripted([LLMProviderError("bad request")])
    provider = _provider(primary, redis, clock=clock)

    with pytest.raises(LLMProviderError, match="bad request"):
        await provider.generate([LLMMessage(role="user", content="hello")])

    assert len(primary.calls) == 1
    assert clock.now == 0


async def test_circuit_breaker_switches_to_fallback_alerts_once_and_probes_after_cooldown() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    primary = MockLLMProvider.scripted(
        [LLMServerError("503")] * 12 + [mock_text_response("primary restored")]
    )
    fallback = MockLLMProvider.scripted([mock_text_response("fallback")])
    alerts: list[str] = []

    async def notify_admin(text: str) -> None:
        alerts.append(text)

    config = ResilientLLMConfig(circuit_cooldown_seconds=10)
    provider = _provider(
        primary,
        redis,
        fallback=fallback,
        clock=clock,
        config=config,
        notify_admin=notify_admin,
        rng=lambda _low, _high: 0,
    )
    request = [LLMMessage(role="user", content="hello")]

    for _ in range(3):
        with pytest.raises(LLMServerError):
            await provider.generate(request)
    fallback_response = await provider.generate(request)

    assert fallback_response.message.content == "fallback"
    assert len(primary.calls) == 12
    assert alerts == ["Основная модель primary/model недоступна; переключаюсь на резервную модель."]

    clock.now += 10
    primary_response = await provider.generate(request)

    assert primary_response.message.content == "primary restored"
    assert len(primary.calls) == 13
    assert await redis.get("cb:openrouter:primary/model") is None


async def test_no_fallback_does_not_create_or_route_a_circuit_breaker() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    primary = MockLLMProvider.scripted([LLMRateLimitError("429")] * 4)
    provider = _provider(primary, redis, clock=clock, rng=lambda _low, _high: 0)

    with pytest.raises(LLMRateLimitError):
        await provider.generate([LLMMessage(role="user", content="hello")])

    assert len(primary.calls) == 4
    assert await redis.get("cb:openrouter:primary/model") is None
