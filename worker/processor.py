"""Worker processor protocol and temporary echo implementation."""

from __future__ import annotations

from typing import Protocol

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from worker.reply import WorkerReply


class Processor(Protocol):
    """Process one queued update and optionally return reply text."""

    async def process(self, envelope: UpdateEnvelope) -> str | WorkerReply | None:
        """Process an update envelope."""


class ContextualProcessor(Protocol):
    """Inner processor that receives the onboarding-resolved task context."""

    async def process(
        self, envelope: UpdateEnvelope, context: TaskContext
    ) -> str | WorkerReply | None:
        """Process one active-user update."""


class EchoProcessor:
    """Temporary processor that echoes text payloads back to the user."""

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        """Return the text payload for text messages, otherwise no reply."""

        del context
        text = envelope.payload.get("text")
        if isinstance(text, str):
            return text
        return None
