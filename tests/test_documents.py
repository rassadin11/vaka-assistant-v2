"""Offline coverage for PDF ingest and LLM-facing document tools."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import fitz
import pytest

from core.context import TaskContext
from core.embeddings import EmbeddingsUnavailableError, MockEmbeddingsProvider
from core.envelope import UpdateEnvelope
from core.tools import ToolRegistry
from tools.documents import (
    DeleteDocumentArgs,
    SearchDocumentsArgs,
    _delete_document,
    _list_documents,
    _search_documents,
    register_document_tools,
)
from worker.documents import (
    EMBEDDINGS_UNAVAILABLE_TEXT,
    LIMIT_APPROACH_PDF_TEXT,
    MAX_DOCUMENT_BYTES,
    MONTHLY_LIMIT_TEXT,
    OCR_PAGE_LIMIT_TEXT,
    SUCCESS_TEXT,
    PdfIngestProcessor,
    _monthly_pages_key,
    chunk_pages,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.documents: list[dict[str, object]] = []
        self.chunks: list[dict[str, object]] = []
        self.next_id = 1

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            return "SELECT 1"
        if "UPDATE documents SET status = 'failed'" in query:
            self._document(int(args[0]))["status"] = "failed"
        elif "UPDATE documents SET status = 'ready'" in query:
            document = self._document(int(args[0]))
            document["status"] = "ready"
            document["pages"] = args[1]
        elif "INSERT INTO doc_chunks" in query:
            self.chunks.append(
                {"doc_id": args[1], "page": args[2], "text": args[4], "tokens": args[5]}
            )
        else:
            raise AssertionError(query)
        return "OK"

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "INSERT INTO documents" not in query:
            raise AssertionError(query)
        document = {
            "id": self.next_id,
            "filename": args[1],
            "status": "processing",
            "pages": None,
            "created_at": datetime(2026, 7, 10, tzinfo=UTC),
        }
        self.next_id += 1
        self.documents.append(document)
        return {"id": document["id"]}

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        if "FROM doc_chunks AS c" in query:
            doc_id = args[1]
            return [
                {
                    "doc_id": chunk["doc_id"],
                    "filename": self._document(int(chunk["doc_id"]))["filename"],
                    "page": chunk["page"],
                    "text": chunk["text"],
                }
                for chunk in self.chunks
                if self._document(int(chunk["doc_id"]))["status"] == "ready"
                and (doc_id is None or chunk["doc_id"] == doc_id)
            ][:6]
        if "FROM documents" in query:
            return list(reversed(self.documents))[:50]
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: object) -> object | None:
        if "DELETE FROM documents" not in query:
            raise AssertionError(query)
        document_id = int(args[0])
        for index, document in enumerate(self.documents):
            if document["id"] == document_id:
                self.documents.pop(index)
                self.chunks = [chunk for chunk in self.chunks if chunk["doc_id"] != document_id]
                return document_id
        return None

    def _document(self, document_id: int) -> dict[str, object]:
        return next(document for document in self.documents if document["id"] == document_id)


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeRedis:
    def __init__(self, pages: int = 0) -> None:
        self.values: dict[str, str] = {}
        self.notice_keys: set[str] = set()
        if pages:
            self.values[_monthly_pages_key(_context())] = str(pages)

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def incrby(self, name: str, amount: int) -> int:
        value = int(self.values.get(name, "0")) + amount
        self.values[name] = str(value)
        return value

    async def set(self, name: str, _value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        del ex
        if nx and name in self.notice_keys:
            return False
        self.notice_keys.add(name)
        return True

    async def get_for_registry(self, name: str) -> str | None:
        return self.values.get(name)

    async def incr(self, name: str) -> int:
        return await self.incrby(name, 1)

    async def expire(self, _name: str, _seconds: int) -> bool:
        return True


class UnavailableEmbeddings:
    async def embed(self, _texts: list[str], _kind: str) -> list[list[float]]:
        raise EmbeddingsUnavailableError


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=100,
        update_id=1,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


def _envelope(**payload: object) -> UpdateEnvelope:
    return UpdateEnvelope(
        update_id=1,
        user_id=100,
        chat_id=100,
        kind="document",
        payload={"tg_file_id": "file", "size": 100, "mime_type": "application/pdf", **payload},
    )


def _pdf(pages: list[str]) -> bytes:
    document = fitz.open()
    try:
        for text in pages:
            page = document.new_page()
            if text:
                page.insert_text((72, 72), text)
        return document.tobytes()
    finally:
        document.close()


async def _download(content: bytes, _file_id: str, _max_bytes: int) -> bytes:
    return content


def test_chunker_preserves_page_boundaries_overlap_and_empty_pages() -> None:
    long_page = "token " * 1000
    chunks = chunk_pages(["", long_page, "last page"])

    assert {chunk.page for chunk in chunks} == {2, 3}
    assert all(chunk.tokens <= 800 for chunk in chunks)
    assert chunks[0].text.split()[-100:] == chunks[1].text.split()[:100]
    assert chunks[-1].page == 3 and chunks[-1].chunk_index == 0


async def test_ingest_rejects_mime_and_size_before_download() -> None:
    pool, redis = FakePool(), FakeRedis()
    processor = PdfIngestProcessor(pool, redis, None, MockEmbeddingsProvider())  # type: ignore[arg-type]

    assert (
        await processor.process(_envelope(mime_type="text/plain"), _context())
        == "Поддерживаются только PDF-файлы"
    )
    assert await processor.process(_envelope(size=MAX_DOCUMENT_BYTES + 1), _context()) == (
        "PDF-файлы больше 20 МБ не поддерживаются"
    )
    assert pool.connection.documents == []


async def test_sparse_pdf_uses_ocr_and_enforces_100_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, redis = FakePool(), FakeRedis()
    processor = PdfIngestProcessor(
        pool,
        redis,
        lambda file_id, maximum: _download(_pdf([""]), file_id, maximum),
        MockEmbeddingsProvider(),
    )
    monkeypatch.setattr("worker.documents._ocr_page", lambda _page: "ocr text")
    assert await processor.process(_envelope(), _context()) == SUCCESS_TEXT.format(pages=1)

    too_many = _pdf([""] * 101)
    processor = PdfIngestProcessor(
        pool,
        redis,
        lambda file_id, maximum: _download(too_many, file_id, maximum),
        MockEmbeddingsProvider(),
    )
    assert await processor.process(_envelope(), _context()) == OCR_PAGE_LIMIT_TEXT


async def test_monthly_counter_rejects_then_increments_only_after_success() -> None:
    content = _pdf(["normal text " * 10])
    pool, capped_redis = FakePool(), FakeRedis(pages=200)
    capped = PdfIngestProcessor(
        pool,
        capped_redis,
        lambda file_id, maximum: _download(content, file_id, maximum),
        MockEmbeddingsProvider(),
    )
    assert await capped.process(_envelope(), _context()) == MONTHLY_LIMIT_TEXT
    assert capped_redis.values[_monthly_pages_key(_context())] == "200"

    pool, redis = FakePool(), FakeRedis()
    ok = PdfIngestProcessor(
        pool,
        redis,
        lambda file_id, maximum: _download(content, file_id, maximum),
        MockEmbeddingsProvider(),
    )
    assert await ok.process(_envelope(), _context()) == SUCCESS_TEXT.format(pages=1)
    assert redis.values[_monthly_pages_key(_context())] == "1"


async def test_embeddings_outage_marks_document_failed() -> None:
    content = _pdf(["normal text " * 10])
    pool, redis = FakePool(), FakeRedis()
    processor = PdfIngestProcessor(
        pool,
        redis,
        lambda file_id, maximum: _download(content, file_id, maximum),
        UnavailableEmbeddings(),  # type: ignore[arg-type]
    )

    assert await processor.process(_envelope(), _context()) == EMBEDDINGS_UNAVAILABLE_TEXT
    assert pool.connection.documents[0]["status"] == "failed"
    assert redis.values == {}


async def test_pdf_limit_approach_is_appended_once_after_eighty_percent() -> None:
    content = _pdf(["normal text " * 10])
    pool, redis = FakePool(), FakeRedis(pages=159)
    processor = PdfIngestProcessor(
        pool,
        redis,
        lambda file_id, maximum: _download(content, file_id, maximum),
        MockEmbeddingsProvider(),
    )

    first = await processor.process(_envelope(), _context())
    second = await processor.process(_envelope(), _context())

    assert first == (
        f"{SUCCESS_TEXT.format(pages=1)}\n\n{LIMIT_APPROACH_PDF_TEXT.format(used=160, limit=200)}"
    )
    assert second == SUCCESS_TEXT.format(pages=1)


async def test_document_search_list_delete_and_registration() -> None:
    pool = FakePool()
    pool.connection.documents.extend(
        [
            {
                "id": 1,
                "filename": "one.pdf",
                "status": "ready",
                "pages": 2,
                "created_at": datetime(2026, 7, 10, tzinfo=UTC),
            },
            {
                "id": 2,
                "filename": "two.pdf",
                "status": "processing",
                "pages": None,
                "created_at": datetime(2026, 7, 11, tzinfo=UTC),
            },
        ]
    )
    pool.connection.chunks.append({"doc_id": 1, "page": 2, "text": "needle", "tokens": 1})
    context = _context()
    found = await _search_documents(
        pool, MockEmbeddingsProvider(), context, SearchDocumentsArgs(query="needle")
    )
    listed = await _list_documents(pool, context)
    deleted = await _delete_document(pool, context, DeleteDocumentArgs(doc_id=1))
    missing = await _delete_document(pool, context, DeleteDocumentArgs(doc_id=999))

    registry = ToolRegistry(FakeRedis(), pool)  # type: ignore[arg-type]
    register_document_tools(registry, pool, MockEmbeddingsProvider())
    assert found.payload["rows"] == [
        {"doc_id": 1, "filename": "one.pdf", "page": 2, "text": "needle"}
    ]
    assert listed.payload["rows"][0]["id"] == 2
    assert "+03:00" in str(listed.payload["rows"][0]["created_at"])
    assert deleted.status == "ok" and pool.connection.chunks == []
    assert missing.status == "error" and missing.retryable is False
    assert {spec.name for spec in registry.get_for_context(context)} == {
        "search_documents",
        "list_documents",
        "delete_document",
    }
