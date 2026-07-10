"""Internal embeddings service.

For a local non-Docker run, keep the Hugging Face cache off the system disk:

    HF_HOME=/d/hf-cache uv run --group embeddings uvicorn embeddings.app:app --port 8090
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DEFAULT_MODEL = "intfloat/multilingual-e5-large"
MAX_BATCH_SIZE = 64


class EmbedRequest(BaseModel):
    """One bounded batch for local embedding inference."""

    texts: list[str] = Field(max_length=MAX_BATCH_SIZE)
    kind: Literal["query", "passage"]


class EmbedResponse(BaseModel):
    """Normalized embedding vectors."""

    vectors: list[list[float]]


def create_app(model_name: str | None = None) -> FastAPI:
    """Build the service while deferring the optional ML import to startup."""

    configured_model = model_name or os.getenv("EMBEDDINGS_MODEL", DEFAULT_MODEL)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        model: Any = SentenceTransformer(configured_model)
        # E5 accepts up to 512 wordpiece tokens.  Explicitly pin this value so
        # service behaviour stays fixed if a model config changes its default.
        model.max_seq_length = min(int(model.max_seq_length), 512)
        app.state.model = model
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        if not hasattr(app.state, "model"):
            raise HTTPException(status_code=503, detail="model is not loaded")
        return {"status": "ok", "model": configured_model}

    @app.post("/embed", response_model=EmbedResponse)
    async def embed(request: EmbedRequest) -> EmbedResponse:
        model: Any = getattr(app.state, "model", None)
        if model is None:
            raise HTTPException(status_code=503, detail="model is not loaded")
        texts = _prefixed_texts(request.texts, request.kind, configured_model)
        encoded = model.encode(texts, normalize_embeddings=True)
        vectors = [[float(item) for item in vector] for vector in encoded.tolist()]
        return EmbedResponse(vectors=vectors)

    return app


def _prefixed_texts(
    texts: list[str], kind: Literal["query", "passage"], model_name: str
) -> list[str]:
    """Apply E5's required retrieval-role prefix, leaving other models neutral."""

    if "e5" not in model_name.lower():
        return texts
    return [f"{kind}: {text}" for text in texts]


app = create_app()
