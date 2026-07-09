"""Unit tests for the queue envelope contract and partitioning."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.envelope import UpdateEnvelope
from core.queue import PARTITION_COUNT, partition_for_user, stream_key


def _envelope(**overrides: object) -> UpdateEnvelope:
    defaults: dict[str, object] = {
        "update_id": 100,
        "user_id": 42,
        "chat_id": 42,
        "kind": "text",
        "payload": {"text": "hello"},
    }
    defaults.update(overrides)
    return UpdateEnvelope.model_validate(defaults)


def test_envelope_stream_roundtrip() -> None:
    original = _envelope()
    restored = UpdateEnvelope.from_stream_entry(original.to_stream_entry())
    assert restored == original


def test_envelope_stream_roundtrip_bytes_keys() -> None:
    original = _envelope(kind="voice", payload={"tg_file_id": "abc", "duration": 3})
    entry = {k.encode("ascii"): v.encode("utf-8") for k, v in original.to_stream_entry().items()}
    restored = UpdateEnvelope.from_stream_entry(entry)
    assert restored == original


def test_envelope_enqueued_at_normalized_to_utc() -> None:
    naive = datetime(2026, 7, 9, 12, 0, 0)
    envelope = _envelope(enqueued_at=naive)
    assert envelope.enqueued_at.tzinfo == UTC


def test_envelope_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _envelope(unexpected="boom")


def test_partition_function_is_stable() -> None:
    # Fixed expected values: changing the hash or PARTITION_COUNT breaks
    # per-user ordering and requires a deliberate queue rebalance.
    assert PARTITION_COUNT == 16
    assert partition_for_user(1) == 7
    assert partition_for_user(42) == 8
    assert partition_for_user(123456789) == 6
    assert partition_for_user(900000777) == 12


def test_stream_key_bounds() -> None:
    assert stream_key("interactive", 0) == "q:interactive:0"
    assert stream_key("background", 15) == "q:background:15"
    with pytest.raises(ValueError, match="partition"):
        stream_key("interactive", 16)
