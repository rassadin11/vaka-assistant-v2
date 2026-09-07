"""Offline coverage for the full text search retrieval backend (small profile)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import pytest

from core.context import TaskContext
from core.embeddings import MockEmbeddingsProvider
from core.profile import RetrievalBackend
from core.tools import ToolRegistry
from tools.documents import SearchDocumentsArgs, _search_documents
from tools.memory import RememberFactArgs, _remember_fact
from tools.registry import register_builtin_tools
from worker.agent_processor import AUTOINJECT_FTS_LIMIT, _load_memory_facts

LOGGER = logging.getLogger(__name__)
EXISTING_FACT_ID = UUID("018f0000-0000-7000-8000-000000000010")


class ExplodingEmbeddings:
    """Any call proves the full text branch reached for embeddings it must not need."""

    async def embed(self, _texts: object, _kind: object) -> list[list[float]]:
        raise AssertionError("the fts backend must not call the embeddings service")


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakeFtsConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeFtsConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeFtsConnection:
    """Answers by looking at the SQL, so each query can be asserted on separately."""

    def __init__(self) -> None:
        self.duplicate_id: UUID | None = None
        self.fact_count = 1
        self.fact_rows: list[dict[str, object]] = []
        self.chunk_batches: list[list[dict[str, object]]] = []
        self.fetched: list[tuple[str, tuple[object, ...]]] = []
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" not in query:
            self.executed.append((query, args))
        return "OK"

    async def fetchval(self, query: str, *_args: object) -> object:
        if "similarity(text" in query:
            return self.duplicate_id
        if "count(*)" in query:
            return self.fact_count
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetched.append((query, args))
        if "doc_chunks" in query:
            return self.chunk_batches.pop(0) if self.chunk_batches else []
        return self.fact_rows


class FakeFtsPool:
    def __init__(self) -> None:
        self.connection = FakeFtsConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeRedis:
    async def get(self, _name: str) -> None:
        return None

    async def set(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def incr(self, _name: str) -> int:
        return 1

    async def expire(self, _name: str, _seconds: int) -> bool:
        return True


def _context() -> TaskContext:
    return TaskContext(
        user_id=UUID("018f0000-0000-7000-8000-000000000001"),
        tg_user_id=100,
        chat_id=500,
        update_id=1,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-000000000002"),
    )


async def test_remember_fact_stores_a_null_embedding_without_the_service() -> None:
    pool = FakeFtsPool()

    result = await _remember_fact(
        pool,
        ExplodingEmbeddings(),
        _context(),
        RememberFactArgs(fact="Люблю варить кофе в турке"),
        retrieval=RetrievalBackend.FTS,
    )

    assert result.payload == {"deduplicated": False}
    query, args = pool.connection.executed[0]
    assert "INSERT INTO memory_facts" in query
    assert args[2] == "Люблю варить кофе в турке"
    assert args[3] is None


async def test_remember_fact_deduplicates_by_trigram_similarity() -> None:
    pool = FakeFtsPool()
    pool.connection.duplicate_id = EXISTING_FACT_ID

    result = await _remember_fact(
        pool,
        None,
        _context(),
        RememberFactArgs(fact="Люблю варить кофе в турке"),
        retrieval=RetrievalBackend.FTS,
    )

    assert result.payload == {"deduplicated": True}
    assert len(pool.connection.executed) == 1
    assert "UPDATE memory_facts" in pool.connection.executed[0][0]
    assert pool.connection.executed[0][1] == (EXISTING_FACT_ID,)


async def test_remember_fact_refuses_the_vector_backend_without_a_provider() -> None:
    with pytest.raises(ValueError, match="embeddings provider"):
        await _remember_fact(
            FakeFtsPool(),
            None,
            _context(),
            RememberFactArgs(fact="Люблю кофе"),
            retrieval=RetrievalBackend.VECTOR,
        )


async def test_autoinject_asks_for_ten_facts_and_marks_them_used() -> None:
    """Twice the vector allowance, as the plan's compensation for lost paraphrases."""

    pool = FakeFtsPool()
    ids = [UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 11)]
    pool.connection.fact_rows = [
        {"id": fact_id, "text": f"fact-{index}"} for index, fact_id in enumerate(ids, start=1)
    ]

    facts = await _load_memory_facts(
        pool,
        _context(),
        "чем я добираюсь до работы",
        ExplodingEmbeddings(),
        LOGGER,
        RetrievalBackend.FTS,
    )

    assert len(facts) == 10
    query, args = pool.connection.fetched[0]
    assert "text_tsv @@" in query
    assert "last_used_at DESC" in query
    assert args == ("чем я добираюсь до работы", AUTOINJECT_FTS_LIMIT)
    assert "UPDATE memory_facts SET last_used_at" in pool.connection.executed[0][0]
    assert pool.connection.executed[0][1] == (ids,)


async def test_autoinject_works_with_no_embeddings_provider_at_all() -> None:
    pool = FakeFtsPool()
    pool.connection.fact_rows = [{"id": EXISTING_FACT_ID, "text": "Аллергия на пенициллин"}]

    facts = await _load_memory_facts(
        pool, _context(), "аллергия", None, LOGGER, RetrievalBackend.FTS
    )

    assert facts == ("Аллергия на пенициллин",)


async def test_autoinject_marks_nothing_when_nothing_matched() -> None:
    pool = FakeFtsPool()

    facts = await _load_memory_facts(pool, _context(), "и в на", None, LOGGER, RetrievalBackend.FTS)

    assert facts == ()
    assert pool.connection.executed == []


async def test_document_search_widens_the_query_only_after_an_empty_result() -> None:
    pool = FakeFtsPool()
    pool.connection.chunk_batches = [
        [],
        [{"doc_id": 7, "filename": "policy.pdf", "page": 3, "text": "фрагмент"}],
    ]

    result = await _search_documents(
        pool,
        None,
        _context(),
        SearchDocumentsArgs(query="страховой случай"),
        retrieval=RetrievalBackend.FTS,
    )

    assert result.payload == {
        "rows": [{"doc_id": 7, "filename": "policy.pdf", "page": 3, "text": "фрагмент"}]
    }
    strict_query, widened_query = (entry[0] for entry in pool.connection.fetched)
    assert "websearch_to_tsquery" in strict_query
    assert "websearch_to_tsquery" not in widened_query
    assert "' | '" in widened_query


async def test_document_search_stops_at_the_strict_query_when_it_matched() -> None:
    pool = FakeFtsPool()
    pool.connection.chunk_batches = [
        [{"doc_id": 7, "filename": "policy.pdf", "page": 3, "text": "фрагмент"}]
    ]

    await _search_documents(
        pool,
        None,
        _context(),
        SearchDocumentsArgs(query="страховой случай"),
        retrieval=RetrievalBackend.FTS,
    )

    assert len(pool.connection.fetched) == 1


def _registered_names(*, embeddings: Any, retrieval: RetrievalBackend) -> set[str]:
    pool = FakeFtsPool()
    redis = FakeRedis()
    registry = ToolRegistry(redis, pool)
    register_builtin_tools(
        registry,
        pool,
        None,
        embeddings,
        redis,
        retrieval=retrieval,
    )
    return {spec.name for spec in registry.get_for_context(_context())}


def test_memory_tools_are_registered_without_embeddings_under_fts() -> None:
    names = _registered_names(embeddings=None, retrieval=RetrievalBackend.FTS)

    assert "remember_fact" in names
    assert "search_documents" in names


def test_memory_tools_stay_unregistered_without_embeddings_under_vector() -> None:
    """The prod profile is unchanged: no embeddings service, no memory tools."""

    names = _registered_names(embeddings=None, retrieval=RetrievalBackend.VECTOR)

    assert "remember_fact" not in names
    assert "search_documents" not in names
    assert "list_documents" in names


def test_memory_tools_are_registered_with_embeddings_under_vector() -> None:
    names = _registered_names(
        embeddings=MockEmbeddingsProvider(), retrieval=RetrievalBackend.VECTOR
    )

    assert "remember_fact" in names
    assert "search_documents" in names
