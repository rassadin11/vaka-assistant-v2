"""Redis-backed per-user worker locks."""

from __future__ import annotations

from redis.asyncio import Redis

DEFAULT_USER_LOCK_TTL_MS = 180_000

_EXTEND_IF_OWNER_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_IF_OWNER_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


def user_lock_key(user_id: int) -> str:
    """Return the Redis key for a user's processing lock."""

    return f"lock:user:{user_id}"


async def acquire_user_lock(
    redis: Redis,
    user_id: int,
    worker_token: str,
    *,
    ttl_ms: int = DEFAULT_USER_LOCK_TTL_MS,
) -> bool:
    """Acquire a per-user lock when no other worker owns it."""

    acquired = await redis.set(user_lock_key(user_id), worker_token, nx=True, px=ttl_ms)
    return bool(acquired)


async def extend_user_lock(
    redis: Redis,
    user_id: int,
    worker_token: str,
    *,
    ttl_ms: int = DEFAULT_USER_LOCK_TTL_MS,
) -> bool:
    """Extend a per-user lock only while it is still owned by this worker."""

    extended = await redis.eval(
        _EXTEND_IF_OWNER_SCRIPT,
        1,
        user_lock_key(user_id),
        worker_token,
        ttl_ms,
    )
    return int(extended) == 1


async def release_user_lock(redis: Redis, user_id: int, worker_token: str) -> bool:
    """Release a per-user lock only while it is still owned by this worker."""

    released = await redis.eval(_RELEASE_IF_OWNER_SCRIPT, 1, user_lock_key(user_id), worker_token)
    return int(released) == 1
