"""Offline tests for the embeddings HTTP boundary."""

from __future__ import annotations

import json

import httpx
import pytest

from core.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingsUnavailableError,
    HttpEmbeddingsProvider,
    MockEmbeddingsProvider,
)


async def test_mock_embeddings_are_deterministic_and_unit_length() -> None:
    provider = MockEmbeddingsProvider()

    first = await provider.embed(["любит кофе"], "passage")
    second = await provider.embed(["любит кофе"], "passage")

    assert first == second
    assert len(first[0]) == EMBEDDING_DIMENSIONS
    assert sum(value * value for value in first[0]) == pytest.approx(1.0)


async def test_http_embeddings_provider_parses_happy_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/embed"
        assert json.loads(request.content) == {"texts": ["hello"], "kind": "query"}
        return httpx.Response(200, json={"vectors": [[0.1, 0.2]]})

    provider = HttpEmbeddingsProvider(
        "http://embeddings.test", transport=httpx.MockTransport(handler)
    )

    assert await provider.embed(["hello"], "query") == [[0.1, 0.2]]


@pytest.mark.parametrize("failure", ["connect", "timeout", "server"])
async def test_http_embeddings_provider_maps_service_failures(failure: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("offline")
        if failure == "timeout":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(503)

    provider = HttpEmbeddingsProvider(
        "http://embeddings.test", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(EmbeddingsUnavailableError):
        await provider.embed(["hello"], "query")
