"""Worker processor protocol and temporary echo implementation."""

from __future__ import annotations

from typing import Protocol

from core.envelope import UpdateEnvelope


class Processor(Protocol):
    """Process one queued update and optionally return reply text."""

    async def process(self, envelope: UpdateEnvelope) -> str | None:
        """Process an update envelope."""


class EchoProcessor:
    """Temporary processor that echoes text payloads back to the user."""

    async def process(self, envelope: UpdateEnvelope) -> str | None:
        """Return the text payload for text messages, otherwise no reply."""

        text = envelope.payload.get("text")
        if isinstance(text, str):
            return text
        return None
