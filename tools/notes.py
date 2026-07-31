"""Topic notes: explicit, user-owned documents addressed by their title."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.db import user_transaction
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

MAX_NOTES_PER_USER = 100
MAX_NOTE_TITLE_LENGTH = 64
MAX_NOTE_CONTENT_LENGTH = 8000
MAX_APPEND_TEXT_LENGTH = 1000
SUGGESTED_TITLES_LIMIT = 10

EMPTY_TITLE_ERROR = "Заголовок заметки не может быть пустым."
TITLE_TOO_LONG_ERROR = f"Заголовок заметки должен быть не длиннее {MAX_NOTE_TITLE_LENGTH} символов."
CONTENT_TOO_LONG_ERROR = (
    f"Текст заметки должен быть не длиннее {MAX_NOTE_CONTENT_LENGTH} символов. "
    "Сократите текст или разбейте тему на несколько заметок."
)
NOTES_LIMIT_ERROR = (
    f"Достигнут лимит {MAX_NOTES_PER_USER} заметок. Предложите пользователю ревизию: "
    "показать список заметок (list_notes) и удалить или объединить неактуальные — "
    "сам или поручив вам."
)
NO_NOTES_ERROR = "У пользователя пока нет заметок."


class NotesConnection(Protocol):
    """Subset of asyncpg used by the note tools."""

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...

    async def execute(self, query: str, *args: Any) -> str: ...


_RESOLVE_EXACT_SQL = """
    SELECT title, content, updated_at
    FROM notes
    WHERE title = $1
"""
_RESOLVE_CASE_INSENSITIVE_SQL = """
    SELECT title, content, updated_at
    FROM notes
    WHERE lower(btrim(title)) = lower($1)
    ORDER BY updated_at DESC, title
    LIMIT 1
"""


class UpsertNoteArgs(BaseModel):
    """Full replacement of one topic note addressed by its title."""

    model_config = ConfigDict(extra="ignore")

    title: str
    content: str = ""


class AppendToNoteArgs(BaseModel):
    """One entry appended to an existing or newly created topic note."""

    model_config = ConfigDict(extra="ignore")

    title: str
    text: str = Field(max_length=MAX_APPEND_TEXT_LENGTH)


class ReadNoteArgs(BaseModel):
    """Title of the note whose full text the model wants to read."""

    model_config = ConfigDict(extra="ignore")

    title: str


class DeleteNoteArgs(BaseModel):
    """Title of the note the user explicitly asked to delete."""

    model_config = ConfigDict(extra="ignore")

    title: str


class ListNotesArgs(BaseModel):
    """Arguments for listing notes without model-controlled values."""

    model_config = ConfigDict(extra="ignore")


def register_notes_tools(registry: ToolRegistry, app_pool: asyncpg.Pool) -> None:
    """Register the five note tools with the worker-owned application pool."""

    async def upsert_note(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _upsert_note(app_pool, ctx, cast(UpsertNoteArgs, args))

    async def append_to_note(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _append_to_note(app_pool, ctx, cast(AppendToNoteArgs, args))

    async def list_notes(ctx: TaskContext, _args: BaseModel) -> ToolResult:
        return await _list_notes(app_pool, ctx)

    async def read_note(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _read_note(app_pool, ctx, cast(ReadNoteArgs, args))

    async def delete_note(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _delete_note(app_pool, ctx, cast(DeleteNoteArgs, args))

    registry.register(
        ToolSpec(
            name="upsert_note",
            description=(
                "Create a topic note or fully replace the text of an existing one, "
                "addressed by its title."
            ),
            args_schema=UpsertNoteArgs,
            risk=RiskLevel.MUTATING_INTERNAL,
            handler=upsert_note,
            daily_limit=30,
        )
    )
    registry.register(
        ToolSpec(
            name="append_to_note",
            description=(
                "Append one entry to a topic note, creating the note when it does not exist yet."
            ),
            args_schema=AppendToNoteArgs,
            risk=RiskLevel.MUTATING_INTERNAL,
            handler=append_to_note,
            daily_limit=60,
        )
    )
    registry.register(
        ToolSpec(
            name="list_notes",
            description="List the titles, sizes, and update times of all the user's notes.",
            args_schema=ListNotesArgs,
            risk=RiskLevel.READ_ONLY,
            handler=list_notes,
        )
    )
    registry.register(
        ToolSpec(
            name="read_note",
            description="Read the full text of one topic note by its title.",
            args_schema=ReadNoteArgs,
            risk=RiskLevel.READ_ONLY,
            handler=read_note,
        )
    )
    registry.register(
        ToolSpec(
            name="delete_note",
            description=(
                "Delete one topic note by its title after the user explicitly asked for it."
            ),
            args_schema=DeleteNoteArgs,
            risk=RiskLevel.MUTATING_INTERNAL,
            handler=delete_note,
            daily_limit=30,
        )
    )


async def _upsert_note(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: UpsertNoteArgs,
) -> ToolResult:
    title = _sanitize_title(args.title)
    title_error = _validate_title(title)
    if title_error is not None:
        return title_error

    content = args.content.strip()
    if len(content) > MAX_NOTE_CONTENT_LENGTH:
        return ToolResult(status="error", error=CONTENT_TOO_LONG_ERROR, retryable=True)

    async with user_transaction(pool, ctx.user_id) as connection:
        existing = await connection.fetchrow(_RESOLVE_EXACT_SQL, title)
        created = existing is None
        if created and await _count_notes(connection) >= MAX_NOTES_PER_USER:
            return ToolResult(status="error", error=NOTES_LIMIT_ERROR, retryable=False)
        await connection.execute(
            """
            INSERT INTO notes (user_id, title, content, created_at, updated_at)
            VALUES ($1, $2, $3, now(), now())
            ON CONFLICT (user_id, title)
            DO UPDATE SET content = EXCLUDED.content, updated_at = now()
            """,
            ctx.user_id,
            title,
            content,
        )
        notes_count = await _count_notes(connection)

    return ToolResult(
        status="ok",
        payload={
            "created": created,
            "notes_count": notes_count,
            "notes_limit": MAX_NOTES_PER_USER,
        },
    )


async def _append_to_note(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: AppendToNoteArgs,
) -> ToolResult:
    title = _sanitize_title(args.title)
    title_error = _validate_title(title)
    if title_error is not None:
        return title_error

    text = args.text.strip()
    if not text:
        return ToolResult(
            status="error",
            error="Текст записи не может быть пустым.",
            retryable=True,
        )

    async with user_transaction(pool, ctx.user_id) as connection:
        existing = await _resolve_note(connection, title)
        created = existing is None
        if created:
            if await _count_notes(connection) >= MAX_NOTES_PER_USER:
                return ToolResult(status="error", error=NOTES_LIMIT_ERROR, retryable=False)
            note_content = text
            if len(note_content) > MAX_NOTE_CONTENT_LENGTH:
                return ToolResult(status="error", error=CONTENT_TOO_LONG_ERROR, retryable=True)
            await connection.execute(
                """
                INSERT INTO notes (user_id, title, content, created_at, updated_at)
                VALUES ($1, $2, $3, now(), now())
                """,
                ctx.user_id,
                title,
                note_content,
            )
            stored_title = title
        else:
            stored_title = _as_str(existing["title"])
            current = _as_str(existing["content"])
            note_content = f"{current}\n{text}" if current else text
            if len(note_content) > MAX_NOTE_CONTENT_LENGTH:
                return ToolResult(
                    status="error",
                    error=_overflow_error(stored_title, len(note_content)),
                    retryable=False,
                )
            await connection.execute(
                "UPDATE notes SET content = $2, updated_at = now() WHERE title = $1",
                stored_title,
                note_content,
            )
        notes_count = await _count_notes(connection)

    return ToolResult(
        status="ok",
        payload={
            "title": stored_title,
            "created": created,
            "note_chars": len(note_content),
            "note_chars_limit": MAX_NOTE_CONTENT_LENGTH,
            "notes_count": notes_count,
            "notes_limit": MAX_NOTES_PER_USER,
        },
    )


async def _list_notes(pool: asyncpg.Pool, ctx: TaskContext) -> ToolResult:
    timezone = ZoneInfo(ctx.timezone)
    async with user_transaction(pool, ctx.user_id) as connection:
        rows = await connection.fetch(
            """
            SELECT title, updated_at, length(content) AS chars
            FROM notes
            ORDER BY updated_at DESC, title
            """
        )
    notes = [
        {
            "title": _as_str(row["title"]),
            "updated_at": _as_datetime(row["updated_at"]).astimezone(timezone).isoformat(),
            "chars": int(cast(int, row["chars"])),
        }
        for row in rows
    ]
    return ToolResult(
        status="ok",
        payload={
            "notes": notes,
            "notes_count": len(notes),
            "notes_limit": MAX_NOTES_PER_USER,
        },
    )


async def _read_note(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: ReadNoteArgs,
) -> ToolResult:
    title = _sanitize_title(args.title)
    title_error = _validate_title(title)
    if title_error is not None:
        return title_error

    timezone = ZoneInfo(ctx.timezone)
    async with user_transaction(pool, ctx.user_id) as connection:
        note = await _resolve_note(connection, title)
        if note is None:
            return await _not_found(connection, title)
        content = _as_str(note["content"])
        return ToolResult(
            status="ok",
            payload={
                "title": _as_str(note["title"]),
                "content": content,
                "chars": len(content),
                "updated_at": _as_datetime(note["updated_at"]).astimezone(timezone).isoformat(),
            },
        )


async def _delete_note(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: DeleteNoteArgs,
) -> ToolResult:
    title = _sanitize_title(args.title)
    title_error = _validate_title(title)
    if title_error is not None:
        return title_error

    async with user_transaction(pool, ctx.user_id) as connection:
        note = await _resolve_note(connection, title)
        if note is None:
            return await _not_found(connection, title)
        await connection.execute("DELETE FROM notes WHERE title = $1", _as_str(note["title"]))
        notes_count = await _count_notes(connection)
    return ToolResult(status="ok", payload={"notes_count": notes_count})


async def _resolve_note(connection: NotesConnection, title: str) -> Any:
    """Resolve a title exactly first, then case-insensitively as the model may recall it."""

    exact = await connection.fetchrow(_RESOLVE_EXACT_SQL, title)
    if exact is not None:
        return exact
    return await connection.fetchrow(_RESOLVE_CASE_INSENSITIVE_SQL, title)


async def _not_found(connection: NotesConnection, title: str) -> ToolResult:
    rows = await connection.fetch(
        """
        SELECT title
        FROM notes
        ORDER BY updated_at DESC, title
        LIMIT $1
        """,
        SUGGESTED_TITLES_LIMIT,
    )
    titles = [_as_str(row["title"]) for row in rows]
    if not titles:
        return ToolResult(
            status="error",
            error=f'Заметка "{title}" не найдена. {NO_NOTES_ERROR}',
            retryable=False,
        )
    suggestions = ", ".join(f'"{item}"' for item in titles)
    return ToolResult(
        status="error",
        error=(
            f'Заметка "{title}" не найдена. Существующие заметки: {suggestions}. '
            "Выберите точный заголовок и повторите вызов."
        ),
        retryable=True,
    )


async def _count_notes(connection: NotesConnection) -> int:
    return int(cast(int, await connection.fetchval("SELECT count(*) FROM notes")))


def _overflow_error(title: str, chars: int) -> str:
    return (
        f'Заметка "{title}" переполнена ({chars}/{MAX_NOTE_CONTENT_LENGTH} символов). '
        "Предложите пользователю ревизию: сократить её, разбить на части или удалить лишнее."
    )


def _validate_title(title: str) -> ToolResult | None:
    if not title:
        return ToolResult(status="error", error=EMPTY_TITLE_ERROR, retryable=True)
    if len(title) > MAX_NOTE_TITLE_LENGTH:
        return ToolResult(status="error", error=TITLE_TOO_LONG_ERROR, retryable=True)
    return None


def _sanitize_title(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _as_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("note column is not a string")
    return value


def _as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("note updated_at is not a datetime")
    return value
