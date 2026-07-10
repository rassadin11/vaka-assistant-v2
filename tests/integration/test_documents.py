"""Live pgvector, cascade, and RLS coverage for document tools."""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg
import pytest

from core.context import TaskContext
from core.db import service_transaction, user_transaction
from core.embeddings import MockEmbeddingsProvider
from tools.documents import (
    DeleteDocumentArgs,
    SearchDocumentsArgs,
    _delete_document,
    _search_documents,
)
from tools.memory import vector_to_literal

pytestmark = pytest.mark.integration


def _context(user_id: UUID) -> TaskContext:
    chat_id = int(user_id.int % 1_000_000_000)
    return TaskContext(
        user_id=user_id,
        tg_user_id=chat_id,
        chat_id=chat_id,
        update_id=1,
        timezone="Europe/Moscow",
        plan="trial",
        trace_id=uuid4(),
    )


async def _create_user(service_pool: asyncpg.Pool, user_id: UUID) -> None:
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


async def _insert_document(
    app_pool: asyncpg.Pool, user_id: UUID, text: str, embeddings: MockEmbeddingsProvider
) -> int:
    vector = vector_to_literal((await embeddings.embed([text], "passage"))[0])
    async with user_transaction(app_pool, user_id) as connection:
        document = await connection.fetchrow(
            """
            INSERT INTO documents (
                user_id, filename, pages, status, tg_file_id, size_bytes, created_at
            )
            VALUES ($1, 'test.pdf', 1, 'ready', 'file', 1, now())
            RETURNING id
            """,
            user_id,
        )
        assert document is not None
        document_id = int(document["id"])
        await connection.execute(
            """
            INSERT INTO doc_chunks (user_id, doc_id, page, chunk_index, text, tokens, embedding)
            VALUES ($1, $2, 1, 0, $3, 1, $4::vector)
            """,
            user_id,
            document_id,
            text,
            vector,
        )
    return document_id


async def test_document_search_delete_cascade_and_rls_isolation(
    app_pool: asyncpg.Pool, service_pool: asyncpg.Pool
) -> None:
    user_a, user_b = uuid4(), uuid4()
    embeddings = MockEmbeddingsProvider()
    await _create_user(service_pool, user_a)
    await _create_user(service_pool, user_b)
    try:
        doc_a = await _insert_document(app_pool, user_a, "private alpha text", embeddings)
        doc_b = await _insert_document(app_pool, user_b, "private beta text", embeddings)
        found = await _search_documents(
            app_pool, embeddings, _context(user_a), SearchDocumentsArgs(query="private alpha text")
        )
        foreign = await _search_documents(
            app_pool,
            embeddings,
            _context(user_a),
            SearchDocumentsArgs(query="private beta text", doc_id=doc_b),
        )
        deleted = await _delete_document(
            app_pool, _context(user_a), DeleteDocumentArgs(doc_id=doc_a)
        )
        async with user_transaction(app_pool, user_a) as connection:
            chunks_after_delete = await connection.fetchval(
                "SELECT count(*) FROM doc_chunks WHERE doc_id = $1", doc_a
            )

        assert found.payload["rows"][0]["doc_id"] == doc_a
        assert foreign.payload["rows"] == []
        assert deleted.status == "ok"
        assert chunks_after_delete == 0
    finally:
        async with service_transaction(service_pool) as connection:
            await connection.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])", [user_a, user_b]
            )
