"""Prometheus metrics and small instrumentation helpers for application services."""

from __future__ import annotations

from decimal import Decimal

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

from core.spend import _usd_rub_rate

TASK_DURATION_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120)


class Metrics:
    """Metrics bound to one registry, primarily to support isolated unit tests."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.updates_received = Counter(
            "updates_received_total",
            "Telegram updates accepted by the gateway.",
            ["kind"],
            registry=registry,
        )
        self.updates_ignored = Counter(
            "updates_ignored_total",
            "Telegram updates ignored by the gateway parser",
            ["reason"],
            registry=registry,
        )
        self.updates_enqueued = Counter(
            "updates_enqueued_total",
            "Telegram updates put onto a worker queue.",
            ["queue"],
            registry=registry,
        )
        self.updates_dedup_skipped = Counter(
            "updates_dedup_skipped_total",
            "Duplicate Telegram updates skipped by the gateway.",
            registry=registry,
        )
        self.updates_rate_limited = Counter(
            "updates_rate_limited_total",
            "Telegram updates rejected by the gateway rate limiter.",
            registry=registry,
        )
        self.task_duration = Histogram(
            "task_duration_seconds",
            "Worker task processing duration.",
            ["queue"],
            buckets=TASK_DURATION_BUCKETS,
            registry=registry,
        )
        self.tasks_processed = Counter(
            "tasks_processed_total",
            "Worker tasks by completed outcome.",
            ["queue", "outcome"],
            registry=registry,
        )
        self.queue_depth = Gauge(
            "queue_depth",
            "Total entries across Redis Streams partitions for a queue.",
            ["queue"],
            registry=registry,
        )
        self.llm_requests = Counter(
            "llm_requests_total",
            "LLM provider requests by result.",
            ["model", "outcome"],
            registry=registry,
        )
        self.llm_fallback = Counter(
            "llm_fallback_total",
            "LLM fallback requests caused by an open circuit breaker.",
            ["from", "to"],
            registry=registry,
        )
        self.llm_cost_rub = Counter(
            "llm_cost_rub_total",
            "Recorded LLM and STT cost in RUB.",
            ["queue"],
            registry=registry,
        )
        self.stt_requests = Counter(
            "stt_requests_total",
            "Speech-to-text requests by result.",
            ["outcome"],
            registry=registry,
        )
        self.tool_calls = Counter(
            "tool_calls_total",
            "Tool calls by tool name and result status.",
            ["tool", "status"],
            registry=registry,
        )


_metrics = Metrics()


def configure_metrics(registry: CollectorRegistry = REGISTRY) -> Metrics:
    """Replace the active metrics registry; intended for isolated unit tests."""

    global _metrics
    _metrics = Metrics(registry)
    return _metrics


def install_metrics(metrics: Metrics) -> None:
    """Install already-created metrics, allowing tests to restore the prior registry."""

    global _metrics
    _metrics = metrics


def active_metrics() -> Metrics:
    """Return the metrics currently used by application instrumentation."""

    return _metrics


def record_llm_cost(cost_usd: Decimal, queue: str) -> None:
    """Add a successfully recorded provider cost using the spend conversion rate."""

    _metrics.llm_cost_rub.labels(queue=queue).inc(float(cost_usd * _usd_rub_rate()))
