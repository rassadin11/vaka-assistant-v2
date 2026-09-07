"""Live Postgres coverage for the full text search retrieval backend (S4)."""

# ruff: noqa: RUF001

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg
import pytest

from core.context import TaskContext
from core.db import service_transaction, user_transaction
from core.profile import RetrievalBackend
from core.retrieval import FACT_TRIGRAM_DUPLICATE_THRESHOLD
from tools.documents import SearchDocumentsArgs, _search_documents
from tools.memory import RememberFactArgs, _remember_fact
from worker.agent_processor import _load_memory_facts

pytestmark = pytest.mark.integration

LOGGER = __import__("logging").getLogger(__name__)

# A realistic personal-assistant memory: what a user actually accumulates.
FACTS = [
    "Ежедневно езжу на работу на велосипеде через набережную",
    "Люблю варить кофе в турке по утрам",
    "У меня аллергия на пенициллин",
    "Жену зовут Марина, день рождения 14 марта",
    "Работаю бэкенд-разработчиком в Яндексе",
    "Дочка Соня ходит в третий класс школы номер 12",
    "Не ем красное мясо, но ем рыбу",
    "Тренируюсь в зале по вторникам и четвергам",
    "Машина — синяя Шкода Октавия, номер А123ВС",
    "Плачу за ипотеку 65 тысяч рублей каждое пятое число",
    "Мама живёт в Воронеже, звоню ей по воскресеньям",
    "Пароли храню в KeePass, мастер-пароль не записываю",
    "Люблю фантастику, особенно Лема и Стругацких",
    "Кота зовут Барсик, ему шесть лет",
    "Отпуск обычно беру в сентябре",
    "Терапевт в поликлинике на Ленина, зовут Ольга Петровна",
    "Изучаю испанский по вторникам с репетитором",
]

CHUNKS = [
    (
        "polis.pdf",
        1,
        "Страховой случай признаётся наступившим при обращении застрахованного "
        "в медицинское учреждение из списка партнёров в течение срока действия полиса.",
    ),
    (
        "polis.pdf",
        2,
        "Возмещение расходов на лекарства производится по чекам в размере "
        "не более двадцати тысяч рублей за календарный год.",
    ),
    (
        "instrukciya.pdf",
        1,
        "Перед первым включением стиральной машины необходимо удалить "
        "транспортировочные болты с задней панели корпуса.",
    ),
    (
        "instrukciya.pdf",
        2,
        "Фильтр насоса рекомендуется очищать не реже одного раза в три месяца, "
        "предварительно отключив машину от электросети.",
    ),
]


def _context(user_id: UUID, chat_id: int) -> TaskContext:
    return TaskContext(
        user_id=user_id,
        tg_user_id=chat_id,
        chat_id=chat_id,
        update_id=1,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=uuid4(),
    )


@pytest.fixture
async def seeded_user(
    app_pool: asyncpg.Pool,
    service_pool: asyncpg.Pool,
) -> object:
    """A user with the sample facts and two indexed documents, all without vectors."""

    user_id = uuid4()
    chat_id = int(user_id.int % 1_000_000_000)
    async with service_transaction(service_pool) as connection:
        await connection.execute(
            """
            INSERT INTO users (
                id, tg_user_id, tg_chat_id, status, timezone, plan, created_at, updated_at
            )
            VALUES ($1, $2, $2, 'active', 'Europe/Moscow', 'trial', now(), now())
            """,
            user_id,
            chat_id,
        )
    context = _context(user_id, chat_id)
    try:
        for fact in FACTS:
            await _remember_fact(
                app_pool,
                None,
                context,
                RememberFactArgs(fact=fact),
                retrieval=RetrievalBackend.FTS,
            )
        async with user_transaction(app_pool, user_id) as connection:
            documents: dict[str, int] = {}
            for filename, page, text in CHUNKS:
                if filename not in documents:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO documents (
                            user_id, filename, tg_file_id, status, pages, size_bytes, created_at
                        )
                        VALUES ($1, $2, $3, 'ready', 2, 1024, now())
                        RETURNING id
                        """,
                        user_id,
                        filename,
                        f"file-{filename}",
                    )
                    documents[filename] = int(row["id"])
                await connection.execute(
                    """
                    INSERT INTO doc_chunks (
                        user_id, doc_id, page, chunk_index, text, tokens, embedding
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, NULL)
                    """,
                    user_id,
                    documents[filename],
                    page,
                    page,
                    text,
                    50,
                )
        yield context
    finally:
        async with service_transaction(service_pool) as connection:
            await connection.execute("DELETE FROM users WHERE id = $1", user_id)


async def test_seventeen_facts_are_stored_without_an_embeddings_service(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    async with user_transaction(app_pool, seeded_user.user_id) as connection:
        total = await connection.fetchval("SELECT count(*) FROM memory_facts")
        without_vectors = await connection.fetchval(
            "SELECT count(*) FROM memory_facts WHERE embedding IS NULL"
        )

    assert total == len(FACTS)
    assert without_vectors == len(FACTS)


async def test_autoinject_finds_facts_by_word_stem_not_by_substring(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    """A whole user message is the query, so its lexemes must be OR-ed, not AND-ed."""

    facts = await _load_memory_facts(
        app_pool,
        seeded_user,
        "Напомни, как зовут мою жену и когда у неё день рождения?",
        None,
        LOGGER,
        RetrievalBackend.FTS,
    )

    assert "Жену зовут Марина, день рождения 14 марта" in facts


async def test_autoinject_marks_the_returned_facts_as_used(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    async with user_transaction(app_pool, seeded_user.user_id) as connection:
        before = await connection.fetchval(
            "SELECT last_used_at FROM memory_facts WHERE text = $1", FACTS[2]
        )

    facts = await _load_memory_facts(
        app_pool, seeded_user, "у меня аллергия на антибиотики", None, LOGGER, RetrievalBackend.FTS
    )

    async with user_transaction(app_pool, seeded_user.user_id) as connection:
        after = await connection.fetchval(
            "SELECT last_used_at FROM memory_facts WHERE text = $1", FACTS[2]
        )

    assert FACTS[2] in facts
    assert after > before


async def test_autoinject_returns_nothing_for_a_message_of_stop_words(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    """An all-stop-word message produces a NULL tsquery, which must match nothing."""

    facts = await _load_memory_facts(
        app_pool, seeded_user, "а и в на же", None, LOGGER, RetrievalBackend.FTS
    )

    assert facts == ()


async def test_autoinject_caps_the_result_at_ten_facts(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    facts = await _load_memory_facts(
        app_pool,
        seeded_user,
        " ".join(FACTS),
        None,
        LOGGER,
        RetrievalBackend.FTS,
    )

    assert len(facts) == 10


async def test_remember_fact_deduplicates_a_typo_but_keeps_a_different_fact(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    typo = await _remember_fact(
        app_pool,
        None,
        seeded_user,
        RememberFactArgs(fact="У меня аллергия на пеницилин"),
        retrieval=RetrievalBackend.FTS,
    )
    different = await _remember_fact(
        app_pool,
        None,
        seeded_user,
        RememberFactArgs(fact="У меня аллергия на кошек"),
        retrieval=RetrievalBackend.FTS,
    )

    async with user_transaction(app_pool, seeded_user.user_id) as connection:
        total = await connection.fetchval("SELECT count(*) FROM memory_facts")

    assert typo.payload == {"deduplicated": True}
    # Same topic, different fact: full text search would call these a match, which
    # is exactly why deduplication uses trigram similarity instead.
    assert different.payload == {"deduplicated": False}
    assert total == len(FACTS) + 1


async def test_document_search_matches_a_phrase_the_user_did_not_quote(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    result = await _search_documents(
        app_pool,
        None,
        seeded_user,
        SearchDocumentsArgs(query="страховой случай"),
        retrieval=RetrievalBackend.FTS,
    )

    rows = result.payload["rows"]
    assert rows
    assert rows[0]["filename"] == "polis.pdf"
    assert rows[0]["page"] == 1


async def test_document_search_falls_back_to_or_when_the_conjunction_fails(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    """No chunk holds every word, so the strict query finds nothing and OR saves it."""

    result = await _search_documents(
        app_pool,
        None,
        seeded_user,
        SearchDocumentsArgs(query="транспортировочные болты гарантия сервис"),
        retrieval=RetrievalBackend.FTS,
    )

    rows = result.payload["rows"]
    assert rows
    assert rows[0]["filename"] == "instrukciya.pdf"


async def test_document_search_respects_the_doc_id_filter(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    everything = await _search_documents(
        app_pool,
        None,
        seeded_user,
        SearchDocumentsArgs(query="машина"),
        retrieval=RetrievalBackend.FTS,
    )
    doc_id = int(everything.payload["rows"][0]["doc_id"])
    filtered = await _search_documents(
        app_pool,
        None,
        seeded_user,
        SearchDocumentsArgs(query="машина", doc_id=doc_id),
        retrieval=RetrievalBackend.FTS,
    )

    assert filtered.payload["rows"]
    assert all(int(row["doc_id"]) == doc_id for row in filtered.payload["rows"])


async def test_documents_of_another_user_stay_invisible(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    stranger = _context(uuid4(), 999_000_001)

    result = await _search_documents(
        app_pool,
        None,
        stranger,
        SearchDocumentsArgs(query="страховой случай"),
        retrieval=RetrievalBackend.FTS,
    )

    assert result.payload["rows"] == []


async def test_the_trigram_threshold_separates_the_measured_sample(
    app_pool: asyncpg.Pool,
    seeded_user: TaskContext,
) -> None:
    """Guard the constant against a silent regression in either direction."""

    duplicates = [
        ("У меня аллергия на пенициллин", "У меня аллергия на пеницилин"),
        ("Кота зовут Барсик, ему шесть лет", "Кота зовут Барсик ему шесть лет"),
    ]
    distinct = [
        ("У меня аллергия на пенициллин", "У меня аллергия на кошек"),
        ("Ежедневно езжу на работу на велосипеде", "Ежедневно езжу на работу на самокате"),
    ]
    async with user_transaction(app_pool, seeded_user.user_id) as connection:
        for left, right in duplicates:
            score = await connection.fetchval("SELECT similarity($1, $2)", left, right)
            assert score >= FACT_TRIGRAM_DUPLICATE_THRESHOLD, (left, right, score)
        for left, right in distinct:
            score = await connection.fetchval("SELECT similarity($1, $2)", left, right)
            assert score < FACT_TRIGRAM_DUPLICATE_THRESHOLD, (left, right, score)
