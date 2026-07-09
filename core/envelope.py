"""Redis queue envelope contract shared by gateway and workers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

EnvelopeKind = Literal["text", "voice", "document", "callback"]
RedisStreamEntry = dict[str | bytes, str | bytes]


class UpdateEnvelope(BaseModel):
    """Stable message contract for Redis Streams queue entries."""

    model_config = ConfigDict(extra="forbid")

    STREAM_FIELD: ClassVar[str] = "envelope"
    VERSION: ClassVar[int] = 1

    version: int = Field(default=VERSION)
    update_id: int
    user_id: int
    chat_id: int
    kind: EnvelopeKind
    payload: dict[str, Any]
    trace_id: UUID = Field(default_factory=uuid4)
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attempt: int = 0

    @field_validator("enqueued_at")
    @classmethod
    def _require_utc_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def to_stream_entry(self) -> dict[str, str]:
        """Serialize the envelope as a single Redis Stream field."""

        return {self.STREAM_FIELD: self.model_dump_json()}

    @classmethod
    def from_stream_entry(cls, entry: RedisStreamEntry) -> UpdateEnvelope:
        """Deserialize an envelope from a Redis Stream field mapping."""

        raw_value = entry.get(cls.STREAM_FIELD)
        if raw_value is None:
            raw_value = entry.get(cls.STREAM_FIELD.encode("ascii"))
        if raw_value is None:
            raise ValueError("Redis Stream entry does not contain an envelope field.")
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8")
        return cls.model_validate_json(raw_value)
