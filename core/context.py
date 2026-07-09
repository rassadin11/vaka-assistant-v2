"""Context trusted by the worker and injected into task execution."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TaskContext:
    """The resolved user identity and request metadata for one task."""

    user_id: UUID
    tg_user_id: int
    chat_id: int
    timezone: str
    trace_id: UUID
