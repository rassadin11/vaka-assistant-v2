"""Service tests run only in the optional embeddings dependency environment."""

from __future__ import annotations

import httpx
import pytest

sentence_transformers = pytest.importorskip("sentence_transformers")


async def test_service_applies_e5_prefixes_and_rejects_oversize_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from embeddings.app import create_app

    captured: list[list[str]] = []

    class FakeModel:
        max_seq_length = 1024

        def encode(self, texts: list[str], *, normalize_embeddings: bool) -> object:
            assert normalize_embeddings is True
            captured.append(texts)

            class Values:
                def tolist(self) -> list[list[float]]:
                    return [[1.0, 0.0]]

            return Values()

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", lambda _name: FakeModel())
    app = create_app("intfloat/multilingual-e5-large")
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://embeddings.test") as client,
    ):
        response = await client.post("/embed", json={"texts": ["привет"], "kind": "query"})
        oversized = await client.post("/embed", json={"texts": ["x"] * 65, "kind": "passage"})

    assert response.json() == {"vectors": [[1.0, 0.0]]}
    assert captured == [["query: привет"]]
    assert oversized.status_code == 422
