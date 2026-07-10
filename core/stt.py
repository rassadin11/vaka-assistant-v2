"""Provider-neutral contracts and Groq implementation for speech-to-text."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx

GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3"
DEFAULT_GROQ_STT_USD_PER_MINUTE = Decimal("0.00185")
GROQ_STT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class STTResult:
    """A successful transcript and its provider-calculated cost."""

    text: str
    duration_seconds: float
    cost_usd: Decimal


class STTProvider(Protocol):
    """Transcribe one audio file into text."""

    async def transcribe(self, audio: bytes, filename: str, language_hint: str = "ru") -> STTResult:
        """Return a transcript for the supplied audio bytes."""


class STTUnavailableError(RuntimeError):
    """Raised when a speech-to-text provider cannot complete a request."""


class GroqSTTProvider:
    """Call Groq's OpenAI-compatible Whisper transcription endpoint once."""

    def __init__(
        self,
        api_key: str,
        *,
        usd_per_minute: Decimal | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._usd_per_minute = (
            usd_per_minute if usd_per_minute is not None else groq_stt_rate_from_env()
        )
        self._transport = transport

    async def transcribe(self, audio: bytes, filename: str, language_hint: str = "ru") -> STTResult:
        """Submit one multipart request and map all provider failures uniformly."""

        try:
            async with httpx.AsyncClient(
                timeout=GROQ_STT_TIMEOUT_SECONDS,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    GROQ_TRANSCRIPTIONS_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data={
                        "model": GROQ_STT_MODEL,
                        "language": language_hint,
                        # verbose_json: the plain json format omits duration,
                        # which the cost calculation requires.
                        "response_format": "verbose_json",
                    },
                    files={"file": (filename, audio, "audio/ogg")},
                )
                response.raise_for_status()
                payload = response.json()
            return _result_from_payload(payload, self._usd_per_minute)
        except (httpx.HTTPError, ValueError, TypeError, InvalidOperation) as exc:
            raise STTUnavailableError("Groq STT request failed") from exc


@dataclass(frozen=True, slots=True)
class MockSTTCall:
    """One call captured by :class:`MockSTTProvider`."""

    audio: bytes
    filename: str
    language_hint: str


class MockSTTProvider:
    """Return scripted STT results while recording input calls for unit tests."""

    def __init__(self, responses: Sequence[STTResult | BaseException]) -> None:
        self._responses = list(responses)
        self.calls: list[MockSTTCall] = []

    @classmethod
    def scripted(cls, responses: Sequence[STTResult | BaseException]) -> MockSTTProvider:
        """Create a mock provider with a deterministic response sequence."""

        return cls(responses)

    async def transcribe(self, audio: bytes, filename: str, language_hint: str = "ru") -> STTResult:
        """Record a request and return the next scripted response."""

        self.calls.append(MockSTTCall(audio, filename, language_hint))
        if not self._responses:
            raise AssertionError("MockSTTProvider script is exhausted.")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def groq_stt_rate_from_env() -> Decimal:
    """Load the Groq per-minute price, retaining the documented default."""

    raw_rate = os.getenv("GROQ_STT_USD_PER_MINUTE", str(DEFAULT_GROQ_STT_USD_PER_MINUTE))
    try:
        return Decimal(raw_rate)
    except InvalidOperation as exc:
        raise ValueError("GROQ_STT_USD_PER_MINUTE must be a decimal number.") from exc


def _result_from_payload(payload: object, usd_per_minute: Decimal) -> STTResult:
    if not isinstance(payload, dict):
        raise ValueError("Groq STT response was not a JSON object.")
    text = payload.get("text")
    duration = payload.get("duration")
    if not isinstance(text, str) or isinstance(duration, bool):
        raise ValueError("Groq STT response omitted text or duration.")
    try:
        duration_decimal = Decimal(str(duration))
        duration_seconds = float(duration_decimal)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError("Groq STT duration was not numeric.") from exc
    if duration_seconds < 0:
        raise ValueError("Groq STT duration was negative.")
    return STTResult(
        text=text,
        duration_seconds=duration_seconds,
        cost_usd=(duration_decimal / Decimal(60)) * usd_per_minute,
    )
