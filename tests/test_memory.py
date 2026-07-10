"""Offline memory tool and block-D retrieval coverage."""

from __future__ import annotations

from uuid import UUID

from core.context import TaskContext
from core.embeddings import EMBEDDING_DIMENSIONS, EmbeddingsUnavailableError, MockEmbeddingsProvider
from tools.memory import RememberFactArgs, _remember_fact
from worker.agent_processor import _load_memory_facts


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakeMemoryConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeMemoryConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeMemoryConnection:
    def __init__(self) -> None:
        self.nearest: dict[str, object] | None = None
        self.fact_count = 0
        self.rows: list[dict[str, object]] = []
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" not in query:
            self.executed.append((query, args))
        return "OK"

    async def fetchrow(self, _query: str, *_args: object) -> dict[str, object] | None:
        return self.nearest

    async def fetchval(self, _query: str, *_args: object) -> int:
        return self.fact_count

    async def fetch(self, _query: str, *_args: object) -> list[dict[str, object]]:
        return self.rows


class FakeMemoryPool:
    def __init__(self) -> None:
        self.connection = FakeMemoryConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class UnavailableEmbeddings:
    async def embed(self, _texts: object, _kind: object) -> list[list[float]]:
        raise EmbeddingsUnavailableError()


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


async def test_remember_fact_deduplicates_similar_fact() -> None:
    pool = FakeMemoryPool()
    existing = UUID("018f0000-0000-7000-8000-000000000010")
    pool.connection.nearest = {"id": existing, "sim": 0.93}

    result = await _remember_fact(
        pool, MockEmbeddingsProvider(), _context(), RememberFactArgs(fact="Любит кофе")
    )

    assert result.payload == {"deduplicated": True}
    assert len(pool.connection.executed) == 1
    assert "UPDATE memory_facts" in pool.connection.executed[0][0]
    assert pool.connection.executed[0][1] == (existing,)


async def test_remember_fact_inserts_a_new_fact() -> None:
    pool = FakeMemoryPool()
    pool.connection.fact_count = 1

    result = await _remember_fact(
        pool, MockEmbeddingsProvider(), _context(), RememberFactArgs(fact="Любит кофе")
    )

    assert result.payload == {"deduplicated": False}
    assert len(pool.connection.executed) == 1
    query, args = pool.connection.executed[0]
    assert "INSERT INTO memory_facts" in query
    assert args[2] == "Любит кофе"
    assert str(args[3]).startswith("[")


async def test_remember_fact_evicts_old_facts_beyond_limit() -> None:
    pool = FakeMemoryPool()
    pool.connection.fact_count = 501

    await _remember_fact(
        pool, MockEmbeddingsProvider(), _context(), RememberFactArgs(fact="Любит кофе")
    )

    assert len(pool.connection.executed) == 2
    delete_query = pool.connection.executed[1][0]
    assert "DELETE FROM memory_facts" in delete_query
    # Keep the 500 most recently used facts; evict the oldest ones (registry §7.1).
    assert "last_used_at DESC" in delete_query
    assert pool.connection.executed[1][1] == (500,)


async def test_remember_fact_returns_retryable_error_when_embeddings_are_unavailable() -> None:
    result = await _remember_fact(
        FakeMemoryPool(), UnavailableEmbeddings(), _context(), RememberFactArgs(fact="Любит кофе")
    )

    assert result.status == "error"
    assert result.retryable is True
    assert result.error == "Память временно недоступна. Попробуйте позже."


async def test_autoinject_filters_to_threshold_and_top_five_then_marks_rows_used() -> None:
    pool = FakeMemoryPool()
    ids = [UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 8)]
    pool.connection.rows = [
        {"id": fact_id, "text": f"fact-{index}", "sim": similarity}
        for index, (fact_id, similarity) in enumerate(
            zip(ids, [0.99, 0.95, 0.90, 0.85, 0.80, 0.79, 0.98], strict=True), start=1
        )
    ]

    facts = await _load_memory_facts(
        pool,
        _context(),
        "question",
        MockEmbeddingsProvider(),
        __import__("logging").getLogger(__name__),
    )

    assert facts == ("fact-1", "fact-2", "fact-3", "fact-4", "fact-5")
    assert len(pool.connection.executed) == 1
    assert "UPDATE memory_facts SET last_used_at" in pool.connection.executed[0][0]
    assert pool.connection.executed[0][1] == (ids[:5],)


async def test_autoinject_skips_when_provider_is_missing_or_unavailable() -> None:
    pool = FakeMemoryPool()
    logger = __import__("logging").getLogger(__name__)

    assert await _load_memory_facts(pool, _context(), "question", None, logger) == ()
    assert (
        await _load_memory_facts(pool, _context(), "question", UnavailableEmbeddings(), logger)
        == ()
    )
    assert pool.connection.executed == []


async def test_autoinject_does_not_need_a_real_vector_provider() -> None:
    class StaticEmbeddings:
        async def embed(self, _texts: object, _kind: object) -> list[list[float]]:
            return [[0.0] * EMBEDDING_DIMENSIONS]

    pool = FakeMemoryPool()
    assert (
        await _load_memory_facts(
            pool,
            _context(),
            "question",
            StaticEmbeddings(),
            __import__("logging").getLogger(__name__),
        )
        == ()
    )
