"""Redis-backed per-user token bucket rate limiting."""

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Protocol

DEFAULT_RATE_LIMIT_PER_MINUTE = 20
DEFAULT_RATE_LIMIT_BURST = 5
DEFAULT_RATE_LIMIT_WINDOW_MS = 60_000
RedisEvalArg = bytes | bytearray | memoryview | str | int | float

RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_tokens = tonumber(ARGV[3])
local refill_window_ms = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])

local tokens = capacity
local last_ms = now_ms
local raw = redis.call("GET", key)
if raw then
  local separator = string.find(raw, ":")
  if separator then
    tokens = tonumber(string.sub(raw, 1, separator - 1)) or capacity
    last_ms = tonumber(string.sub(raw, separator + 1)) or now_ms
  end
end

local elapsed_ms = math.max(0, now_ms - last_ms)
tokens = math.min(capacity, tokens + (elapsed_ms * refill_tokens / refill_window_ms))

local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end

redis.call("SET", key, tostring(tokens) .. ":" .. tostring(now_ms), "PX", ttl_ms)
return { allowed, tokens }
"""


class RateLimitRedis(Protocol):
    """Subset of Redis commands needed for the token bucket script."""

    def eval(self, script: str, numkeys: int, *keys_and_args: RedisEvalArg) -> Awaitable[object]:
        """Evaluate a Lua script."""
        ...


def rate_limit_key(user_id: int) -> str:
    """Return the cache key for a user's token bucket."""

    return f"rl:user:{user_id}"


async def allow_user_update(
    redis: RateLimitRedis,
    user_id: int,
    *,
    per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    burst: int = DEFAULT_RATE_LIMIT_BURST,
    refill_window_ms: int = DEFAULT_RATE_LIMIT_WINDOW_MS,
    now_ms: int | None = None,
) -> bool:
    """Atomically consume one token from a user's Redis token bucket."""

    if per_minute <= 0:
        raise ValueError("per_minute must be positive.")
    if burst <= 0:
        raise ValueError("burst must be positive.")
    if refill_window_ms <= 0:
        raise ValueError("refill_window_ms must be positive.")

    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    empty_to_full_ms = int((burst * refill_window_ms / per_minute) + 0.999)
    ttl_ms = max(refill_window_ms, empty_to_full_ms) * 2
    result = await redis.eval(
        RATE_LIMIT_SCRIPT,
        1,
        rate_limit_key(user_id),
        current_ms,
        burst,
        per_minute,
        refill_window_ms,
        ttl_ms,
    )
    return _allowed_from_eval_result(result)


def _allowed_from_eval_result(result: object) -> bool:
    if isinstance(result, list | tuple):
        return bool(_int_result(result[0]))
    return bool(_int_result(result))


def _int_result(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float | str | bytes | bytearray):
        return int(value)
    raise TypeError(f"unexpected Redis eval result value: {value!r}")
