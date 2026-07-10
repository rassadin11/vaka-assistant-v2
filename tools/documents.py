"""Document search and lifecycle tools backed by the documents tables."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.db import user_transaction
from core.embeddings import EmbeddingsProvider, EmbeddingsUnavailableError
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec
from tools.memory import vector_to_literal


class SearchDocumentsArgs(BaseModel):
    """Arguments for semantic document search."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=200)
    doc_id: int | None = None


class ListDocumentsArgs(BaseModel):
    """Arguments for listing documents; intentionally empty."""

    model_config = ConfigDict(extra="ignore")


class DeleteDocumentArgs(BaseModel):
    """The trusted document id selected by the model."""

    model_config = ConfigDict(extra="ignore")

    doc_id: int


def register_document_tools(
    registry: ToolRegistry,
    app_pool: asyncpg.Pool,
    embeddings: EmbeddingsProvider | None,
) -> None:
    """Register always-available lifecycle tools and optional semantic search."""

    async def list_documents(ctx: TaskContext, _args: BaseModel) -> ToolResult:
        return await _list_documents(app_pool, ctx)

    async def delete_document(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _delete_document(app_pool, ctx, cast(DeleteDocumentArgs, args))

    registry.register(
        ToolSpec(
            name="list_documents",
            description="List the user's uploaded PDF documents and their processing status.",
            args_schema=ListDocumentsArgs,
            risk=RiskLevel.READ_ONLY,
            handler=list_documents,
        )
    )
    registry.register(
        ToolSpec(
            name="delete_document",
            description="Delete one uploaded document and all of its indexed chunks.",
            args_schema=DeleteDocumentArgs,
            risk=RiskLevel.MUTATING_INTERNAL,
            handler=delete_document,
        )
    )
    if embeddings is None:
        return

    async def search_documents(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _search_documents(app_pool, embeddings, ctx, cast(SearchDocumentsArgs, args))

    registry.register(
        ToolSpec(
            name="search_documents",
            description="Search the user's ready PDF documents for relevant passages and pages.",
            args_schema=SearchDocumentsArgs,
            risk=RiskLevel.READ_ONLY,
            handler=search_documents,
        )
    )


async def _search_documents(
    pool: asyncpg.Pool,
    embeddings: EmbeddingsProvider,
    context: TaskContext,
    args: SearchDocumentsArgs,
) -> ToolResult:
    try:
        vectors = await embeddings.embed([args.query], "query")
    except EmbeddingsUnavailableError:
        return ToolResult(
            status="error",
            error="Поиск по документам временно недоступен",
            retryable=True,
        )
    if len(vectors) != 1:
        raise ValueError("embeddings provider returned an unexpected number of vectors")
    vector = vector_to_literal(vectors[0])
    async with user_transaction(pool, context.user_id) as connection:
        records = await connection.fetch(
            """
            SELECT c.doc_id, d.filename, c.page, c.text
            FROM doc_chunks AS c
            JOIN documents AS d ON d.id = c.doc_id
            WHERE d.status = 'ready'
              AND ($2::bigint IS NULL OR c.doc_id = $2)
            ORDER BY c.embedding <=> $1::vector
            LIMIT 6
            """,
            vector,
            args.doc_id,
        )
    rows = [
        {
            "doc_id": int(record["doc_id"]),
            "filename": str(record["filename"]),
            "page": int(record["page"]),
            "text": str(record["text"]),
        }
        for record in records
    ]
    return ToolResult(status="ok", payload={"rows": rows})


async def _list_documents(pool: asyncpg.Pool, context: TaskContext) -> ToolResult:
    async with user_transaction(pool, context.user_id) as connection:
        records = await connection.fetch(
            """
            SELECT id, filename, pages, status, created_at
            FROM documents
            ORDER BY created_at DESC
            LIMIT 50
            """
        )
    timezone = ZoneInfo(context.timezone)
    rows = [
        {
            "id": int(record["id"]),
            "filename": str(record["filename"]),
            "pages": int(record["pages"]) if record["pages"] is not None else None,
            "status": str(record["status"]),
            "created_at": _localized_iso(record["created_at"], timezone),
        }
        for record in records
    ]
    return ToolResult(status="ok", payload={"rows": rows})


async def _delete_document(
    pool: asyncpg.Pool,
    context: TaskContext,
    args: DeleteDocumentArgs,
) -> ToolResult:
    async with user_transaction(pool, context.user_id) as connection:
        deleted = await connection.fetchval(
            "DELETE FROM documents WHERE id = $1 RETURNING id", args.doc_id
        )
    if deleted is None:
        return ToolResult(status="error", error="Документ не найден.", retryable=False)
    return ToolResult(status="ok", payload={"doc_id": args.doc_id})


def _localized_iso(value: object, timezone: ZoneInfo) -> str:
    if not isinstance(value, datetime):
        raise ValueError("documents.created_at must be a datetime")
    return value.astimezone(timezone).isoformat()
