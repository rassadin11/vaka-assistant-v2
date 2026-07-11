"""Unit tests for shared structured logging and trace propagation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from core.logging_setup import setup_logging
from core.tracing import reset_trace_id, set_trace_id


def _json_records(captured: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured.splitlines() if line]


def test_json_logging_includes_required_fields(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    setup_logging("worker")

    logging.getLogger("tests.logging").info("processed update")

    record = _json_records(capsys.readouterr().err)[0]
    assert record["ts"].endswith("+00:00")
    assert record["level"] == "INFO"
    assert record["logger"] == "tests.logging"
    assert record["service"] == "worker"
    assert record["trace_id"] == "-"
    assert record["message"] == "processed update"


def test_json_logging_includes_extra_fields(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    setup_logging("gateway")

    logging.getLogger("tests.logging").info("queued", extra={"update_id": 12, "user_id": 34})

    record = _json_records(capsys.readouterr().err)[0]
    assert record["update_id"] == 12
    assert record["user_id"] == 34


def test_trace_id_priority_extra_context_and_fallback(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    setup_logging("worker")
    logger = logging.getLogger("tests.logging")

    logger.info("fallback")
    token = set_trace_id("context")
    try:
        logger.info("context")
        logger.info("extra", extra={"trace_id": "extra"})
    finally:
        reset_trace_id(token)

    assert [record["trace_id"] for record in _json_records(capsys.readouterr().err)] == [
        "-",
        "context",
        "extra",
    ]


async def test_asyncio_tasks_keep_trace_ids_isolated(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    setup_logging("worker")
    logger = logging.getLogger("tests.logging")

    async def log_with_trace(trace_id: str) -> None:
        token = set_trace_id(trace_id)
        try:
            await asyncio.sleep(0)
            logger.info("task")
        finally:
            reset_trace_id(token)

    await asyncio.gather(log_with_trace("first"), log_with_trace("second"))

    assert {record["trace_id"] for record in _json_records(capsys.readouterr().err)} == {
        "first",
        "second",
    }


def test_text_logging_preserves_legacy_format(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("LOG_FORMAT", "text")
    setup_logging("worker")

    logging.getLogger("tests.logging").info("processed", extra={"trace_id": "trace-1"})

    line = capsys.readouterr().err.strip()
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO "
        r"trace_id=trace-1 tests\.logging: processed",
        line,
    )


def test_non_serializable_extra_value_is_stringified(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    setup_logging("worker")
    value = object()

    logging.getLogger("tests.logging").info("processed", extra={"value": value})

    assert _json_records(capsys.readouterr().err)[0]["value"] == str(value)
