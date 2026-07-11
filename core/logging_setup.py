"""Shared structured logging configuration for service processes."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from core.tracing import current_trace_id

_TEXT_FORMAT = "%(asctime)s %(levelname)s trace_id=%(trace_id)s %(name)s: %(message)s"
_JSON_FIELD_NAMES = frozenset(
    {"ts", "level", "logger", "service", "trace_id", "message", "exc_info"}
)
_LOG_RECORD_ATTRIBUTE_NAMES = frozenset(logging.makeLogRecord({}).__dict__) | frozenset(
    {"asctime", "exc_text", "message"}
)
_MISSING = object()


def setup_logging(service: str) -> None:
    """Configure root logging for one service process."""

    log_format = os.getenv("LOG_FORMAT", "json").lower()
    log_level = logging.getLevelNamesMapping().get(
        os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO
    )
    formatter = _TextFormatter(_TEXT_FORMAT) if log_format == "text" else _JsonFormatter(service)

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=log_level, handlers=[handler], force=True)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.trace_id = _trace_id_for_record(record)
        return super().format(record)


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "trace_id": _trace_id_for_record(record),
            "message": record.getMessage(),
        }
        for name, value in record.__dict__.items():
            if name not in _LOG_RECORD_ATTRIBUTE_NAMES and name not in _JSON_FIELD_NAMES:
                payload[name] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _trace_id_for_record(record: logging.LogRecord) -> str:
    value = record.__dict__.get("trace_id", _MISSING)
    if value is _MISSING:
        value = current_trace_id()
    return "-" if value is None else str(value)
