"""Redis-backed reliability controls for LLM providers."""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from core.llm import (
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponse,
    LLMServerError,
    ToolDefinition,
)
from core.metrics import active_metrics

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
RandomUniform = Callable[[float, float], float]
NotifyAdmin = Callable[[str], Awaitable[None]]

SEMAPHORE_KEY = "sem:openrouter"
SEMAPHORE_TTL_SECONDS = 180
SEMAPHORE_ACQUIRE_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then
    return 0
end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""
SEMAPHORE_RELEASE_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current <= 0 then
    return 0
end
local remaining = redis.call('DECR', KEYS[1])
if remaining < 0 then
    redis.call('SET', KEYS[1], '0')
    return 0
end
return remaining
"""


class ResilientRedis(Protocol):
    """Redis operations used by :class:`ResilientLLMProvider`."""

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Awaitable[Any]:
        """Evaluate a Lua script."""

    def get(self, name: str) -> Awaitable[Any]:
        """Return a value by key."""

    def incr(self, name: str) -> Awaitable[Any]:
        """Increment a numeric key."""

    def expire(self, name: str, time: int) -> Awaitable[Any]:
        """Set a key expiry."""

    def delete(self, *names: Any) -> Awaitable[Any]:
        """Delete keys."""

    def set(
        self,
        name: Any,
        value: Any,
        *,
        ex: Any = None,
        nx: bool = False,
    ) -> Awaitable[Any]:
        """Set a key, optionally only if it does not already exist."""


@dataclass(frozen=True, slots=True)
class ResilientLLMConfig:
    """Limits and timings for the Redis-backed LLM reliability controls."""

    max_concurrency: int = 8
    semaphore_ttl_seconds: int = SEMAPHORE_TTL_SECONDS
    semaphore_wait_seconds: float = 30
    semaphore_poll_seconds: float = 0.2
    retry_count: int = 3
    retry_base_seconds: float = 1
    retry_factor: float = 2
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 300

    @classmethod
    def from_env(cls) -> ResilientLLMConfig:
        """Load reliability limits from environment variables."""

        return cls(
            max_concurrency=int(os.getenv("OPENROUTER_MAX_CONCURRENCY", "8")),
            semaphore_ttl_seconds=int(os.getenv("OPENROUTER_SEMAPHORE_TTL_SECONDS", "180")),
            semaphore_wait_seconds=float(os.getenv("OPENROUTER_SEMAPHORE_WAIT_SECONDS", "30")),
            semaphore_poll_seconds=float(os.getenv("OPENROUTER_SEMAPHORE_POLL_SECONDS", "0.2")),
            retry_count=int(os.getenv("OPENROUTER_RETRY_COUNT", "3")),
            retry_base_seconds=float(os.getenv("OPENROUTER_RETRY_BASE_SECONDS", "1")),
            retry_factor=float(os.getenv("OPENROUTER_RETRY_FACTOR", "2")),
            circuit_failure_threshold=int(os.getenv("OPENROUTER_CIRCUIT_FAILURE_THRESHOLD", "3")),
            circuit_cooldown_seconds=int(os.getenv("OPENROUTER_CIRCUIT_COOLDOWN_SECONDS", "300")),
        )


class ResilientLLMProvider:
    """Wrap an LLM provider with global limiting, retrying, and fallback routing."""

    def __init__(
        self,
        primary_provider: LLMProvider,
        redis: ResilientRedis,
        primary_model: str,
        *,
        fallback_provider: LLMProvider | None = None,
        fallback_model: str | None = None,
        config: ResilientLLMConfig | None = None,
        notify_admin: NotifyAdmin | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        rng: RandomUniform | None = None,
    ) -> None:
        self._primary_provider = primary_provider
        self._fallback_provider = fallback_provider
        self._fallback_model = fallback_model or "fallback"
        self._redis = redis
        self._primary_model = primary_model
        self._config = config if config is not None else ResilientLLMConfig.from_env()
        self._notify_admin = notify_admin
        self._clock = clock if clock is not None else _monotonic
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._rng = rng if rng is not None else random.uniform

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Generate a response using the primary provider or an open fallback."""

        if self._fallback_provider is not None and await self._circuit_is_open():
            active_metrics().llm_fallback.labels(self._primary_model, self._fallback_model).inc()
            return await self._generate_with_retries(
                self._fallback_provider,
                self._fallback_model,
                messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        try:
            response = await self._generate_with_retries(
                self._primary_provider,
                self._primary_model,
                messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except (LLMRateLimitError, LLMServerError):
            if self._fallback_provider is not None:
                await self._record_primary_failure()
            raise

        if self._fallback_provider is not None:
            await self._redis.delete(self._circuit_key)
        return response

    @property
    def _circuit_key(self) -> str:
        return f"cb:openrouter:{self._primary_model}"

    @property
    def _alert_key(self) -> str:
        return f"cb:alerted:{self._primary_model}"

    async def _circuit_is_open(self) -> bool:
        value = await self._redis.get(self._circuit_key)
        return _as_int(value) >= self._config.circuit_failure_threshold

    async def _record_primary_failure(self) -> None:
        failures = _as_int(await self._redis.incr(self._circuit_key))
        await self._redis.expire(self._circuit_key, self._config.circuit_cooldown_seconds)
        if failures < self._config.circuit_failure_threshold or self._notify_admin is None:
            return
        alerted = await self._redis.set(
            self._alert_key,
            "1",
            ex=self._config.circuit_cooldown_seconds,
            nx=True,
        )
        if alerted:
            await self._notify_admin(
                "Основная модель "
                f"{self._primary_model} недоступна; переключаюсь на резервную модель."
            )

    async def _generate_with_retries(
        self,
        provider: LLMProvider,
        model: str,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[ToolDefinition] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> LLMResponse:
        for attempt in range(self._config.retry_count + 1):
            try:
                return await self._generate_once(
                    provider,
                    model,
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except (LLMRateLimitError, LLMServerError):
                if attempt == self._config.retry_count:
                    raise
                upper_bound = self._config.retry_base_seconds * (self._config.retry_factor**attempt)
                await self._sleep(self._rng(0, upper_bound))
        raise AssertionError("retry loop must return or raise")

    async def _generate_once(
        self,
        provider: LLMProvider,
        model: str,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[ToolDefinition] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> LLMResponse:
        await self._acquire_semaphore()
        try:
            response = await provider.generate(
                messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            active_metrics().llm_requests.labels(model=model, outcome=_llm_outcome(exc)).inc()
            raise
        else:
            active_metrics().llm_requests.labels(model=response.model, outcome="ok").inc()
            return response
        finally:
            await self._release_semaphore()

    async def _acquire_semaphore(self) -> None:
        started_at = self._clock()
        while True:
            acquired = await self._redis.eval(
                SEMAPHORE_ACQUIRE_LUA,
                1,
                SEMAPHORE_KEY,
                self._config.max_concurrency,
                self._config.semaphore_ttl_seconds,
            )
            if _as_int(acquired) == 1:
                return
            elapsed = self._clock() - started_at
            if elapsed >= self._config.semaphore_wait_seconds:
                raise LLMProviderError("OpenRouter global concurrency limit is busy.")
            await self._sleep(
                min(
                    self._config.semaphore_poll_seconds,
                    self._config.semaphore_wait_seconds - elapsed,
                )
            )

    async def _release_semaphore(self) -> None:
        await self._redis.eval(SEMAPHORE_RELEASE_LUA, 1, SEMAPHORE_KEY)


def _as_int(value: object | None) -> int:
    if isinstance(value, bytes):
        value = value.decode()
    try:
        return int(cast("str | int | float", value))
    except (TypeError, ValueError):
        return 0


def _llm_outcome(error: Exception) -> str:
    """Map provider errors to the bounded Prometheus outcome label set."""

    if isinstance(error, LLMRateLimitError):
        return "429"
    if isinstance(error, LLMServerError):
        return "5xx"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "other"


def _monotonic() -> float:
    return asyncio.get_running_loop().time()
