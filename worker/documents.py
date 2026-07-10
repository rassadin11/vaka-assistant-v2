"""Asynchronous PDF ingestion for document envelopes."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Protocol

import asyncpg
import fitz
import pytesseract
import tiktoken
from PIL import Image

from core.context import TaskContext
from core.db import user_transaction
from core.embeddings import EmbeddingsProvider, EmbeddingsUnavailableError
from core.envelope import UpdateEnvelope
from core.tokens import count_tokens
from tools.memory import vector_to_literal

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_MONTHLY_PAGES = 200
MONTHLY_KEY_TTL_SECONDS = 62 * 86_400
MAX_OCR_PAGES = 100
OCR_TEXT_THRESHOLD = 50
CHUNK_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 100
EMBEDDING_BATCH_SIZE = 64

PDF_ONLY_TEXT = "Поддерживаются только PDF-файлы"
TOO_LARGE_TEXT = "PDF-файлы больше 20 МБ не поддерживаются"
OCR_UNAVAILABLE_TEXT = "Распознавание сканов временно недоступно"
EMBEDDINGS_UNAVAILABLE_TEXT = "Обработка документов временно недоступна, пришлите файл позже"
SUCCESS_TEXT = "Документ обработан, {pages} страниц. Спрашивайте."
MONTHLY_LIMIT_TEXT = "Превышен месячный лимит обработки документов."
OCR_PAGE_LIMIT_TEXT = "Сканированные PDF больше 100 страниц пока не поддерживаются."
DOWNLOAD_UNAVAILABLE_TEXT = "Скачивание документов временно недоступно, пришлите файл позже."
INVALID_PDF_TEXT = "Не удалось обработать PDF-файл."

LOGGER = logging.getLogger(__name__)
DownloadFile = Callable[[str, int], Awaitable[bytes]]


class QueueRedis(Protocol):
    """Subset of the queue Redis client used for document page accounting."""

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...

    def incrby(self, name: str, amount: int) -> Awaitable[int]: ...

    def expire(self, name: str, seconds: int) -> Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """One page-local chunk ready for embedding and persistence."""

    page: int
    chunk_index: int
    text: str
    tokens: int


class PdfIngestProcessor:
    """Ingest a single user PDF without allowing malformed files to reach the DLQ."""

    def __init__(
        self,
        app_pool: asyncpg.Pool,
        queue_redis: QueueRedis,
        download_file: DownloadFile | None,
        embeddings: EmbeddingsProvider | None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._app_pool = app_pool
        self._queue_redis = queue_redis
        self._download_file = download_file
        self._embeddings = embeddings
        self._logger = logger if logger is not None else LOGGER

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        """Handle one document envelope and convert every processing fault to a reply."""

        payload = envelope.payload
        mime_type = payload.get("mime_type")
        if mime_type != "application/pdf":
            return PDF_ONLY_TEXT
        size = payload.get("size")
        if not isinstance(size, int) or size < 0 or size > MAX_DOCUMENT_BYTES:
            return TOO_LARGE_TEXT
        file_id = payload.get("tg_file_id")
        if not isinstance(file_id, str) or file_id == "":
            return DOWNLOAD_UNAVAILABLE_TEXT
        if self._download_file is None:
            return DOWNLOAD_UNAVAILABLE_TEXT

        filename = payload.get("file_name")
        document_id: int | None = None
        try:
            document_id = await self._create_document(context, filename, file_id, size)
            content = await self._download_file(file_id, MAX_DOCUMENT_BYTES)
            if len(content) > MAX_DOCUMENT_BYTES:
                raise ValueError("download exceeded the document size limit")
            page_count = await self._page_count(content)
            if not await self._within_page_limit(context, page_count):
                await self._mark_failed(context, document_id)
                return MONTHLY_LIMIT_TEXT

            pages = await self._extract_pages(content)

            chunks = chunk_pages(pages)
            if self._embeddings is None:
                await self._mark_failed(context, document_id)
                return EMBEDDINGS_UNAVAILABLE_TEXT
            try:
                vectors = await self._embed_chunks(chunks)
            except EmbeddingsUnavailableError:
                await self._mark_failed(context, document_id)
                return EMBEDDINGS_UNAVAILABLE_TEXT
            await self._insert_chunks(context, document_id, chunks, vectors)
            await self._mark_ready(context, document_id, page_count)
            key = _monthly_pages_key(context)
            if await self._queue_redis.incrby(key, page_count) == page_count:
                # First increment this month: cap the key lifetime past the month end.
                await self._queue_redis.expire(key, MONTHLY_KEY_TTL_SECONDS)
            return SUCCESS_TEXT.format(pages=page_count)
        except _OcrUnavailableError:
            if document_id is not None:
                await self._mark_failed(context, document_id)
            return OCR_UNAVAILABLE_TEXT
        except _OcrPageLimitError:
            if document_id is not None:
                await self._mark_failed(context, document_id)
            return OCR_PAGE_LIMIT_TEXT
        except Exception:
            self._logger.exception("PDF ingest failed", extra={"update_id": envelope.update_id})
            if document_id is not None:
                await self._mark_failed(context, document_id)
            return INVALID_PDF_TEXT

    async def _create_document(
        self,
        context: TaskContext,
        raw_filename: object,
        file_id: str,
        size: int,
    ) -> int:
        filename = (
            raw_filename if isinstance(raw_filename, str) and raw_filename else "document.pdf"
        )
        async with user_transaction(self._app_pool, context.user_id) as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO documents (
                    user_id, filename, status, tg_file_id, size_bytes, created_at
                )
                VALUES ($1, $2, 'processing', $3, $4, now())
                RETURNING id
                """,
                context.user_id,
                filename,
                file_id,
                size,
            )
        if row is None:
            raise RuntimeError("documents insert returned no id")
        return int(row["id"])

    async def _extract_pages(self, content: bytes) -> list[str]:
        # PDF parsing and OCR are CPU-bound: keep them off the worker event loop.
        return await asyncio.to_thread(_parse_pages, content)

    async def _page_count(self, content: bytes) -> int:
        return await asyncio.to_thread(_count_pages, content)

    async def _within_page_limit(self, context: TaskContext, pages: int) -> bool:
        raw_count = await self._queue_redis.get(_monthly_pages_key(context))
        count = int(raw_count.decode("utf-8") if isinstance(raw_count, bytes) else raw_count or "0")
        return count + pages <= MAX_MONTHLY_PAGES

    async def _embed_chunks(self, chunks: Sequence[DocumentChunk]) -> list[list[float]]:
        if self._embeddings is None:
            raise EmbeddingsUnavailableError("embeddings disabled")
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
            batch = chunks[start : start + EMBEDDING_BATCH_SIZE]
            result = await self._embeddings.embed([chunk.text for chunk in batch], "passage")
            if len(result) != len(batch):
                raise ValueError("embeddings provider returned an unexpected number of vectors")
            vectors.extend(result)
        return vectors

    async def _insert_chunks(
        self,
        context: TaskContext,
        document_id: int,
        chunks: Sequence[DocumentChunk],
        vectors: Sequence[list[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunk and embedding counts differ")
        async with user_transaction(self._app_pool, context.user_id) as connection:
            for chunk, vector in zip(chunks, vectors, strict=True):
                await connection.execute(
                    """
                    INSERT INTO doc_chunks (
                        user_id, doc_id, page, chunk_index, text, tokens, embedding
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                    """,
                    context.user_id,
                    document_id,
                    chunk.page,
                    chunk.chunk_index,
                    chunk.text,
                    chunk.tokens,
                    vector_to_literal(vector),
                )

    async def _mark_ready(self, context: TaskContext, document_id: int, pages: int) -> None:
        async with user_transaction(self._app_pool, context.user_id) as connection:
            await connection.execute(
                "UPDATE documents SET status = 'ready', pages = $2 WHERE id = $1",
                document_id,
                pages,
            )

    async def _mark_failed(self, context: TaskContext, document_id: int) -> None:
        try:
            async with user_transaction(self._app_pool, context.user_id) as connection:
                await connection.execute(
                    "UPDATE documents SET status = 'failed' WHERE id = $1", document_id
                )
        except Exception:
            self._logger.exception(
                "failed to mark document ingest as failed", extra={"doc_id": document_id}
            )


def chunk_pages(pages: Sequence[str]) -> list[DocumentChunk]:
    """Split non-empty page text into 800-token windows without crossing pages."""

    encoding = tiktoken.get_encoding("cl100k_base")
    chunks: list[DocumentChunk] = []
    for page_number, page_text in enumerate(pages, start=1):
        if page_text.strip() == "":
            continue
        token_ids = encoding.encode(page_text)
        start = 0
        chunk_index = 0
        while start < len(token_ids):
            end = min(start + CHUNK_TOKENS, len(token_ids))
            text = encoding.decode(token_ids[start:end])
            chunks.append(
                DocumentChunk(
                    page=page_number,
                    chunk_index=chunk_index,
                    text=text,
                    tokens=count_tokens(text),
                )
            )
            if end == len(token_ids):
                break
            start = end - CHUNK_OVERLAP_TOKENS
            chunk_index += 1
    return chunks


def _parse_pages(content: bytes) -> list[str]:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ValueError("invalid PDF") from exc
    try:
        page_count = document.page_count
        pages = [page.get_text() for page in document]
        has_sparse_text = (
            page_count > 0 and sum(len(page) for page in pages) / page_count < OCR_TEXT_THRESHOLD
        )
        if has_sparse_text:
            if page_count > MAX_OCR_PAGES:
                raise _OcrPageLimitError
            pages = [_ocr_page(page) for page in document]
        return pages
    finally:
        document.close()


def _count_pages(content: bytes) -> int:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ValueError("invalid PDF") from exc
    try:
        return int(document.page_count)
    finally:
        document.close()


def _ocr_page(page: fitz.Page) -> str:
    tesseract_cmd = os.getenv("TESSERACT_CMD", "").strip()
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        pixmap = page.get_pixmap(dpi=200)
        image = Image.open(BytesIO(pixmap.tobytes("png")))
        return str(pytesseract.image_to_string(image, lang="rus+eng"))
    except (pytesseract.TesseractNotFoundError, FileNotFoundError) as exc:
        raise _OcrUnavailableError from exc


def _monthly_pages_key(context: TaskContext) -> str:
    return f"doc_pages:{context.user_id}:{datetime.now(UTC):%Y%m}"


class _OcrUnavailableError(Exception):
    """The optional system tesseract executable cannot be invoked."""


class _OcrPageLimitError(Exception):
    """A scanned PDF exceeds the configured OCR page limit."""
