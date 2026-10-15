"""Topic notes: explicit, user-owned documents addressed by their title."""

from __future__ import annotations

from typing import cast
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from core.context import TaskContext
from core.db import user_transaction
from core.notes_service import (
    CONTENT_TOO_LONG_ERROR,
    LIST_NOTES_LIMIT,
    MAX_APPEND_TEXT_LENGTH,
    MAX_NOTE_CONTENT_LENGTH,
    NO_NOTES_ERROR,
    InvalidNoteTitle,
    NoteContentTooLong,
    NoteOverflow,
    NotesConnection,
    NotesLimitReached,
    append_to_note_by_title,
    delete_note_by_title,
    list_notes_for_tool,
    notes_limit,
    notes_limit_message,
    overflow_message,
    resolve_note,
    sanitize_title,
    suggest_titles,
    upsert_note_by_title,
    validate_content,
    validate_title,
)
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec


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
    title = sanitize_title(args.title)
    content = args.content.strip()
    try:
        validate_title(title)
        validate_content(content)
        async with user_transaction(pool, ctx.user_id) as connection:
            outcome = await upsert_note_by_title(connection, ctx.user_id, title, content)
    except (InvalidNoteTitle, NoteContentTooLong, NotesLimitReached) as exc:
        return _error_result(exc)

    return ToolResult(
        status="ok",
        payload={
            "created": outcome.created,
            "notes_count": outcome.notes_count,
            "notes_limit": notes_limit(),
        },
    )


async def _append_to_note(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: AppendToNoteArgs,
) -> ToolResult:
    title = sanitize_title(args.title)
    try:
        validate_title(title)
    except InvalidNoteTitle as exc:
        return _error_result(exc)

    text = args.text.strip()
    if not text:
        return ToolResult(
            status="error",
            error="Текст записи не может быть пустым.",
            retryable=True,
        )

    try:
        async with user_transaction(pool, ctx.user_id) as connection:
            outcome = await append_to_note_by_title(connection, ctx.user_id, title, text)
    except (NotesLimitReached, NoteContentTooLong, NoteOverflow) as exc:
        return _error_result(exc)

    return ToolResult(
        status="ok",
        payload={
            "title": outcome.title,
            "created": outcome.created,
            "note_chars": outcome.note_chars,
            "note_chars_limit": MAX_NOTE_CONTENT_LENGTH,
            "notes_count": outcome.notes_count,
            "notes_limit": notes_limit(),
        },
    )


def _error_result(exc: Exception) -> ToolResult:
    """Map a note domain failure to the model-facing error text of the tools."""

    if isinstance(exc, InvalidNoteTitle):
        return ToolResult(status="error", error=exc.message, retryable=True)
    if isinstance(exc, NoteContentTooLong):
        return ToolResult(status="error", error=CONTENT_TOO_LONG_ERROR, retryable=True)
    if isinstance(exc, NotesLimitReached):
        return ToolResult(status="error", error=notes_limit_message(exc.limit), retryable=False)
    if isinstance(exc, NoteOverflow):
        return ToolResult(
            status="error",
            error=overflow_message(exc.title, exc.chars),
            retryable=False,
        )
    raise exc


async def _list_notes(pool: asyncpg.Pool, ctx: TaskContext) -> ToolResult:
    """List the most recently updated notes, capped to fit the context budget."""

    timezone = ZoneInfo(ctx.timezone)
    async with user_transaction(pool, ctx.user_id) as connection:
        page = await list_notes_for_tool(connection, LIST_NOTES_LIMIT)
    notes = [
        {
            "title": item.title,
            "updated_at": item.updated_at.astimezone(timezone).isoformat(),
            "chars": item.chars,
        }
        for item in page.items
    ]
    return ToolResult(
        status="ok",
        payload={
            "notes": notes,
            # notes_count stays the size of this page; notes_total says how many
            # exist, so the model knows when it is looking at a truncated view
            # and can reach for read_note (prompt rule 16h).
            "notes_count": len(notes),
            "notes_total": page.total,
            "notes_limit": notes_limit(),
        },
    )


async def _read_note(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: ReadNoteArgs,
) -> ToolResult:
    title = sanitize_title(args.title)
    try:
        validate_title(title)
    except InvalidNoteTitle as exc:
        return _error_result(exc)

    timezone = ZoneInfo(ctx.timezone)
    async with user_transaction(pool, ctx.user_id) as connection:
        note = await resolve_note(connection, title)
        if note is None:
            return await _not_found(connection, title)
        return ToolResult(
            status="ok",
            payload={
                "title": note.title,
                "content": note.content,
                "chars": len(note.content),
                "updated_at": note.updated_at.astimezone(timezone).isoformat(),
            },
        )


async def _delete_note(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: DeleteNoteArgs,
) -> ToolResult:
    title = sanitize_title(args.title)
    try:
        validate_title(title)
    except InvalidNoteTitle as exc:
        return _error_result(exc)

    async with user_transaction(pool, ctx.user_id) as connection:
        notes_count = await delete_note_by_title(connection, title)
        if notes_count is None:
            return await _not_found(connection, title)
    return ToolResult(status="ok", payload={"notes_count": notes_count})


async def _not_found(connection: NotesConnection, title: str) -> ToolResult:
    titles = await suggest_titles(connection)
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
