"""Worker configuration loaded from environment variables."""

from __future__ import annotations

import os

from worker.app import WorkerConfig


def config_from_env() -> WorkerConfig:
    """Load worker configuration from environment variables."""

    return WorkerConfig(
        consumer_name=os.getenv("WORKER_CONSUMER_NAME") or WorkerConfig().consumer_name,
        interactive_only=_env_bool("WORKER_INTERACTIVE_ONLY", default=False),
        reclaim_interval_seconds=float(os.getenv("WORKER_RECLAIM_INTERVAL_SECONDS", "30")),
        reclaim_min_idle_ms=int(os.getenv("WORKER_RECLAIM_MIN_IDLE_MS", "210000")),
        max_deliveries=int(os.getenv("WORKER_MAX_DELIVERIES", "3")),
        lock_ttl_ms=int(os.getenv("WORKER_LOCK_TTL_MS", "180000")),
        lock_extend_interval_seconds=float(os.getenv("WORKER_LOCK_EXTEND_INTERVAL_SECONDS", "60")),
        process_timeout_seconds=float(os.getenv("WORKER_PROCESS_TIMEOUT_SECONDS", "120")),
    )


def _env_bool(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}
