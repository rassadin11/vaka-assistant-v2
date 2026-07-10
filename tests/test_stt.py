"""Offline tests for the Groq STT HTTP boundary."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from core.stt import GROQ_TRANSCRIPTIONS_URL, GroqSTTProvider, STTUnavailableError


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_groq_stt_posts_multipart_and_calculates_decimal_cost() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GROQ_TRANSCRIPTIONS_URL
        assert request.headers["authorization"] == "Bearer test-key"
        body = await request.aread()
        assert b'name="model"\r\n\r\nwhisper-large-v3' in body
        assert b'name="language"\r\n\r\nru' in body
        assert b'name="response_format"\r\n\r\nverbose_json' in body
        assert b'name="file"; filename="voice.ogg"' in body
        return httpx.Response(200, json={"text": "привет", "duration": 120})

    provider = GroqSTTProvider(
        "test-key", usd_per_minute=Decimal("0.00185"), transport=_transport(handler)
    )

    result = await provider.transcribe(b"opus", "voice.ogg")

    assert result.text == "привет"
    assert result.duration_seconds == 120.0
    assert result.cost_usd == Decimal("0.00370")


@pytest.mark.parametrize("status_code", [500, 429])
async def test_groq_stt_maps_http_errors_to_unavailable(status_code: int) -> None:
    provider = GroqSTTProvider(
        "test-key",
        transport=_transport(lambda _request: httpx.Response(status_code)),
    )

    with pytest.raises(STTUnavailableError):
        await provider.transcribe(b"opus", "voice.ogg")


async def test_groq_stt_maps_timeout_to_unavailable() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    provider = GroqSTTProvider("test-key", transport=_transport(handler))

    with pytest.raises(STTUnavailableError):
        await provider.transcribe(b"opus", "voice.ogg")
