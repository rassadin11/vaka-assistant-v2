"""Prometheus metrics specific to the Mini App HTTP surface."""

from __future__ import annotations

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram


class WebAppMetrics:
    """Metrics bound to one registry so unit tests can use isolated registries."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.requests = Counter(
            "webapp_requests_total",
            "Mini App HTTP requests by normalized route and status.",
            ["route", "status"],
            registry=registry,
        )
        self.request_duration = Histogram(
            "webapp_request_duration_seconds",
            "Mini App HTTP request duration by normalized route.",
            ["route"],
            registry=registry,
        )
        self.auth_failures = Counter(
            "webapp_auth_failures_total",
            "Mini App authentication failures by safe reason.",
            ["reason"],
            registry=registry,
        )
        self.app_opened = Counter(
            "webapp_app_opened_total",
            "Successful Mini App authentications (session opens).",
            registry=registry,
        )
        self.rate_limited = Counter(
            "webapp_rate_limited_total",
            "Mini App protected API requests rejected by rate limiting.",
            registry=registry,
        )
        self.reminders_created = Counter(
            "webapp_reminders_created_total",
            "One-off reminders created through the Mini App.",
            registry=registry,
        )
        self.reminders_cancelled = Counter(
            "webapp_reminders_cancelled_total",
            "Scheduled tasks cancelled through the Mini App.",
            registry=registry,
        )
        self.transactions_deleted = Counter(
            "webapp_transactions_deleted_total",
            "Transactions deleted through the Mini App.",
            registry=registry,
        )
        self.ai_summary = Counter(
            "webapp_ai_summary_total",
            "Finance AI summary requests by bounded outcome.",
            ["outcome"],
            registry=registry,
        )


default_metrics = WebAppMetrics()
