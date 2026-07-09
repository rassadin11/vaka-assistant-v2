"""Unit tests for Redis-backed per-user locks."""

from __future__ import annotations

from typing import Any

from core.locks import acquire_user_lock, extend_user_lock, release_user_lock, user_lock_key


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool = False,
        px: int | None = None,
    ) -> bool:
        if nx and name in self.values:
            return False
        self.values[name] = value
        if px is not None:
            self.ttls[name] = px
        return True

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        del numkeys
        key = str(keys_and_args[0])
        token = str(keys_and_args[1])
        if self.values.get(key) != token:
            return 0
        if "PEXPIRE" in script:
            self.ttls[key] = int(keys_and_args[2])
            return 1
        if "DEL" in script:
            del self.values[key]
            return 1
        raise AssertionError(f"unexpected script: {script}")


async def test_acquire_extend_release_owned_lock() -> None:
    redis = FakeRedis()

    assert await acquire_user_lock(redis, 42, "token-a", ttl_ms=1000)
    assert redis.values[user_lock_key(42)] == "token-a"
    assert redis.ttls[user_lock_key(42)] == 1000

    assert await extend_user_lock(redis, 42, "token-a", ttl_ms=2000)
    assert redis.ttls[user_lock_key(42)] == 2000

    assert await release_user_lock(redis, 42, "token-a")
    assert user_lock_key(42) not in redis.values


async def test_lock_operations_reject_non_owner() -> None:
    redis = FakeRedis()

    assert await acquire_user_lock(redis, 42, "token-a")
    assert not await acquire_user_lock(redis, 42, "token-b")
    assert not await extend_user_lock(redis, 42, "token-b", ttl_ms=2000)
    assert not await release_user_lock(redis, 42, "token-b")
    assert redis.values[user_lock_key(42)] == "token-a"
