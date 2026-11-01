"""Worker shutdown must always finish so a crashed worker exits and is restarted."""

from __future__ import annotations

import asyncio

from redis.exceptions import TimeoutError as RedisTimeoutError

from worker.__main__ import _queue_depth_collector, _stall_watchdog, stop_background_tasks


class CancelSwallowingRedis:
    """Mimic redis-py turning a cancel inside its connect timeout into TimeoutError."""

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()

    async def xinfo_groups(self, key: str) -> list[dict[object, object]]:
        del key
        self.calls += 1
        self.entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            assert current is not None
            current.uncancel()
            raise RedisTimeoutError("Timeout connecting to server") from None
        return []


async def test_collector_stops_even_when_redis_swallows_the_cancel() -> None:
    queue_redis = CancelSwallowingRedis()
    stop = asyncio.Event()
    task = asyncio.create_task(_queue_depth_collector(queue_redis, stop))  # type: ignore[arg-type]
    await asyncio.wait_for(queue_redis.entered.wait(), timeout=1)

    stop.set()
    await stop_background_tasks([task], grace_seconds=0.2)

    assert task.done()


async def test_stop_background_tasks_cancels_a_loop_that_ignores_stop() -> None:
    async def stubborn() -> None:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

    async def finished() -> None:
        return None

    stubborn_task = asyncio.create_task(stubborn())
    finished_task = asyncio.create_task(finished())

    await asyncio.wait_for(
        stop_background_tasks([stubborn_task, finished_task], grace_seconds=0.05),
        timeout=1,
    )

    assert stubborn_task.done()
    assert finished_task.done()


async def test_stop_background_tasks_gives_up_on_a_task_that_never_ends() -> None:
    release = asyncio.Event()

    async def unkillable() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    task = asyncio.create_task(unkillable())
    await asyncio.sleep(0)

    await asyncio.wait_for(stop_background_tasks([task], grace_seconds=0.05), timeout=1)

    assert not task.done()
    release.set()
    task.cancel()
    await asyncio.wait({task}, timeout=1)


class FakeProgress:
    def __init__(self, stalled_for: float) -> None:
        self.stalled_for = stalled_for

    def seconds_since_progress(self) -> float:
        return self.stalled_for


async def test_watchdog_exits_when_the_consume_loop_stalls() -> None:
    stalls: list[str] = []

    await asyncio.wait_for(
        _stall_watchdog(
            FakeProgress(stalled_for=601),  # type: ignore[arg-type]
            600,
            asyncio.Event(),
            check_seconds=0.01,
            on_stall=lambda: stalls.append("exit"),
        ),
        timeout=1,
    )

    assert stalls == ["exit"]


async def test_watchdog_stays_quiet_while_the_loop_progresses_and_stops_on_signal() -> None:
    stalls: list[str] = []
    stop = asyncio.Event()
    task = asyncio.create_task(
        _stall_watchdog(
            FakeProgress(stalled_for=5),  # type: ignore[arg-type]
            600,
            stop,
            check_seconds=0.01,
            on_stall=lambda: stalls.append("exit"),
        )
    )
    await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert stalls == []
