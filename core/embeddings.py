"""HTTP and test implementations of the embeddings boundary."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Any, Literal, Protocol

import httpx

EmbeddingKind = Literal["query", "passage"]
EMBEDDING_DIMENSIONS = 1024


class EmbeddingsUnavailableError(Exception):
    """The internal embeddings service could not complete a request."""


class EmbeddingsProvider(Protocol):
    """Produce normalized vectors for query or passage text."""

    async def embed(self, texts: Sequence[str], kind: EmbeddingKind) -> list[list[float]]: ...


class HttpEmbeddingsProvider:
    """Use the internal embeddings service without retrying failed calls."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def embed(self, texts: Sequence[str], kind: EmbeddingKind) -> list[list[float]]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post("/embed", json={"texts": list(texts), "kind": kind})
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise EmbeddingsUnavailableError("embeddings service unavailable") from exc

        if response.status_code >= 500:
            raise EmbeddingsUnavailableError("embeddings service returned a server error")
        response.raise_for_status()
        return _vectors_from_response(response.json())


class MockEmbeddingsProvider:
    """Offline deterministic vectors with the production vector dimensionality."""

    async def embed(self, texts: Sequence[str], kind: EmbeddingKind) -> list[list[float]]:
        del kind
        return [_mock_vector(text) for text in texts]


def _vectors_from_response(value: Any) -> list[list[float]]:
    if not isinstance(value, dict) or not isinstance(value.get("vectors"), list):
        raise ValueError("invalid embeddings response")
    vectors: list[list[float]] = []
    for vector in value["vectors"]:
        if not isinstance(vector, list) or not all(
            isinstance(item, int | float) for item in vector
        ):
            raise ValueError("invalid embeddings vector")
        vectors.append([float(item) for item in vector])
    return vectors


def _mock_vector(text: str) -> list[float]:
    values: list[float] = []
    for index in range(EMBEDDING_DIMENSIONS):
        digest = hashlib.sha256(f"{text}:{index}".encode()).digest()
        values.append((int.from_bytes(digest[:8], "big") / 2**63) - 1.0)
    magnitude = math.sqrt(sum(value * value for value in values))
    return [value / magnitude for value in values]
