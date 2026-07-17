"""Unit tests for Prometheus instrumentation and worker queue-depth collection."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from prometheus_client import CollectorRegistry, generate_latest

from core.metrics import active_metrics, configure_metrics, install_metrics
from core.queue import RedisSettings, stream_key
from gateway.app import create_app, handle_update
from gateway.config import GatewayConfig
from worker.__main__ import collect_queue_depths, start_worker_metrics_server


@pytest.fixture
def metric_registry() -> Iterator[CollectorRegistry]:
    """Use a fresh registry without leaking instrumentation into other tests."""

    previous = active_metrics()
    registry = CollectorRegistry()
    configure_metrics(registry)
    try:
        yield registry
    finally:
        install_metrics(previous)


class FakeQueueRedis:
    """Tiny queue client for gateway and queue-depth tests."""

    async def ping(self) -> bool:
        return True

    async def xlen(self, key: str) -> int:
        return 2 if key.endswith(":0") else 0


class FakeCacheRedis:
    """Tiny cache client that always admits a gateway update."""

    async def ping(self) -> bool:
        return True

    async def exists(self, _name: str) -> int:
        return 0

    async def eval(self, _script: str, _numkeys: int, *_args: object) -> list[int]:
        return [1, 0]

    async def set(self, _name: str, _value: str, **_kwargs: object) -> bool:
        return True


async def _enqueue(_redis: object, _queue: str, _envelope: object) -> str:
    return "1-0"


def _config() -> GatewayConfig:
    return GatewayConfig(
        webhook_secret_path="secret",
        telegram_webhook_secret_token="token",
        redis=RedisSettings(queue_url="redis://unused", cache_url="redis://unused"),
        port=8000,
        public_url=None,
        admin_ids=(),
        rate_limit_per_minute=20,
        rate_limit_burst=5,
    )


def _update() -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "from": {"id": 1},
            "chat": {"id": 1, "type": "private"},
            "text": "hello",
        },
    }


async def test_gateway_increments_update_metrics(metric_registry: CollectorRegistry) -> None:
    await handle_update(
        _update(),
        queue_redis=FakeQueueRedis(),
        cache_redis=FakeCacheRedis(),
        enqueue_func=_enqueue,
    )

    exposition = generate_latest(metric_registry).decode()
    assert 'updates_received_total{kind="text"} 1.0' in exposition
    assert 'updates_enqueued_total{queue="interactive"} 1.0' in exposition


async def test_gateway_counts_unsupported_messages_as_ignored(
    metric_registry: CollectorRegistry,
) -> None:
    await handle_update(
        {
            "update_id": 2,
            "message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "sticker": {"file_id": "sticker-file"},
            },
        },
        queue_redis=FakeQueueRedis(),
        cache_redis=FakeCacheRedis(),
        enqueue_func=_enqueue,
    )

    exposition = generate_latest(metric_registry).decode()
    assert 'updates_ignored_total{reason="unsupported_message"} 1.0' in exposition


def test_worker_counter_and_histogram_are_registered_in_isolated_registry(
    metric_registry: CollectorRegistry,
) -> None:
    metrics = active_metrics()
    metrics.task_duration.labels(queue="interactive").observe(1.25)
    metrics.tasks_processed.labels(queue="interactive", outcome="ok").inc()

    exposition = generate_latest(metric_registry).decode()
    assert 'task_duration_seconds_bucket{le="2.0",queue="interactive"} 1.0' in exposition
    assert 'tasks_processed_total{outcome="ok",queue="interactive"} 1.0' in exposition


async def test_gateway_metrics_endpoint_returns_prometheus_exposition(
    metric_registry: CollectorRegistry,
) -> None:
    app = create_app(config=_config(), queue_redis=FakeQueueRedis(), cache_redis=FakeCacheRedis())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "updates_received_total" in response.text
    assert "task_duration_seconds" in response.text


async def test_queue_depth_collector_sums_all_streams(metric_registry: CollectorRegistry) -> None:
    await collect_queue_depths(FakeQueueRedis())  # type: ignore[arg-type]

    exposition = generate_latest(metric_registry).decode()
    assert 'queue_depth{queue="interactive"} 2.0' in exposition
    assert 'queue_depth{queue="background"} 2.0' in exposition
    assert stream_key("interactive", 0) == "q:interactive:0"


def test_worker_metrics_port_zero_does_not_start_server(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []
    monkeypatch.setenv("WORKER_METRICS_PORT", "0")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("worker.__main__.start_http_server", lambda port: started.append(port))

    start_worker_metrics_server()

    assert started == []
