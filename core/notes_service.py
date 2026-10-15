"""Shared topic-note domain logic for the LLM tools and the Mini App."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

import asyncpg

from core.profile import profile_limits

MAX_NOTE_TITLE_LENGTH = 64
# A context budget for the model, not a storage budget: list_notes spends about
# 25-30 tokens per row, and 300 rows would blow the 6-8k budget for a whole
# request (plan S10). It applies to the tools only, never to the Mini App.
LIST_NOTES_LIMIT = 50
MAX_NOTE_CONTENT_LENGTH = 8000
MAX_APPEND_TEXT_LENGTH = 1000
SUGGESTED_TITLES_LIMIT = 10

EMPTY_TITLE_ERROR = "Заголовок заметки не может быть пустым."
TITLE_TOO_LONG_ERROR = f"Заголовок заметки должен быть не длиннее {MAX_NOTE_TITLE_LENGTH} символов."
CONTENT_TOO_LONG_ERROR = (
    f"Текст заметки должен быть не длиннее {MAX_NOTE_CONTENT_LENGTH} символов. "
    "Сократите текст или разбейте тему на несколько заметок."
)
NOTES_LIMIT_ERROR_TEMPLATE = (
    "Достигнут лимит {limit} заметок. Предложите пользователю ревизию: "
    "показать список заметок (list_notes) и удалить или объединить неактуальные — "
    "сам или поручив вам."
)
NO_NOTES_ERROR = "У пользователя пока нет заметок."


class NotesConnection(Protocol):
    """Subset of asyncpg used by the note service."""

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...

    async def execute(self, query: str, *args: Any) -> str: ...


class NoteError(Exception):
    """Base class of expected note domain failures."""


class InvalidNoteTitle(NoteError):
    """The sanitized title is empty or too long."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NoteContentTooLong(NoteError):
    """The note text exceeds the storage limit."""


class NoteOverflow(NoteError):
    """Appending would push an existing note over the storage limit."""

    def __init__(self, title: str, chars: int) -> None:
        super().__init__(title)
        self.title = title
        self.chars = chars


class NotesLimitReached(NoteError):
    """The user already has the maximum number of notes."""

    def __init__(self, limit: int) -> None:
        super().__init__(str(limit))
        self.limit = limit


class NoteTitleTaken(NoteError):
    """Another note already uses this title (compared without regard to case)."""


class NoteNotFound(NoteError):
    """No visible note has the requested id."""


class NoteStale(NoteError):
    """The note changed after the version the caller based its edit on."""

    def __init__(self, current: NoteRecord) -> None:
        super().__init__("stale")
        self.current = current


@dataclass(frozen=True, slots=True)
class ResolvedNote:
    """A note found by title: what the tools need to read or append."""

    title: str
    content: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NoteRecord:
    """One full note row addressed by id."""

    id: int
    title: str
    content: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NoteTitleSummary:
    """One list row for the tools (addressed by title, no id)."""

    title: str
    updated_at: datetime
    chars: int


@dataclass(frozen=True, slots=True)
class NoteSummary:
    """One list row for the Mini App."""

    id: int
    title: str
    updated_at: datetime
    chars: int


@dataclass(frozen=True, slots=True)
class ToolNoteList:
    """A possibly capped page of titles plus the real total."""

    items: tuple[NoteTitleSummary, ...]
    total: int


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
    """Result of a title-addressed full replacement."""

    created: bool
    notes_count: int


@dataclass(frozen=True, slots=True)
class AppendOutcome:
    """Result of a title-addressed append."""

    title: str
    created: bool
    note_chars: int
    notes_count: int


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
_NOTE_COLUMNS = "id, title, content, created_at, updated_at"


def notes_limit() -> int:
    """Return the per-user note cap of the active deployment profile."""

    return profile_limits().notes_per_user


def sanitize_title(value: str) -> str:
    """Trim a title and collapse inner whitespace runs."""

    return re.sub(r"\s+", " ", value.strip())


def validate_title(title: str) -> None:
    """Raise InvalidNoteTitle when a sanitized title is empty or too long."""

    if not title:
        raise InvalidNoteTitle(EMPTY_TITLE_ERROR)
    if len(title) > MAX_NOTE_TITLE_LENGTH:
        raise InvalidNoteTitle(TITLE_TOO_LONG_ERROR)


def validate_content(content: str) -> None:
    """Raise NoteContentTooLong when the text exceeds the storage limit."""

    if len(content) > MAX_NOTE_CONTENT_LENGTH:
        raise NoteContentTooLong


def notes_limit_message(limit: int) -> str:
    """Text for the model when the note cap is reached."""

    return NOTES_LIMIT_ERROR_TEMPLATE.format(limit=limit)


def overflow_message(title: str, chars: int) -> str:
    """Text for the model when an append would overflow a note."""

    return (
        f'Заметка "{title}" переполнена ({chars}/{MAX_NOTE_CONTENT_LENGTH} символов). '
        "Предложите пользователю ревизию: сократить её, разбить на части или удалить лишнее."
    )


async def count_notes(connection: NotesConnection) -> int:
    """Count the notes visible to the current RLS user."""

    return int(cast(int, await connection.fetchval("SELECT count(*) FROM notes")))


async def resolve_note(
    connection: NotesConnection,
    title: str,
    *,
    for_update: bool = False,
) -> ResolvedNote | None:
    """Resolve a title exactly first, then case-insensitively as the model may recall it.

    ``for_update`` locks the resolved row so a read-modify-write caller cannot
    overwrite a concurrent Mini App edit committed between its read and write.
    """

    suffix = " FOR UPDATE" if for_update else ""
    row = await connection.fetchrow(f"{_RESOLVE_EXACT_SQL}{suffix}", title)
    if row is None:
        row = await connection.fetchrow(f"{_RESOLVE_CASE_INSENSITIVE_SQL}{suffix}", title)
    if row is None:
        return None
    return ResolvedNote(
        title=_as_str(row["title"]),
        content=_as_str(row["content"]),
        updated_at=_as_datetime(row["updated_at"]),
    )


async def suggest_titles(connection: NotesConnection) -> list[str]:
    """Return the most recently updated titles to help the model recover from a miss."""

    rows = await connection.fetch(
        """
        SELECT title
        FROM notes
        ORDER BY updated_at DESC, title
        LIMIT $1
        """,
        SUGGESTED_TITLES_LIMIT,
    )
    return [_as_str(row["title"]) for row in rows]


async def upsert_note_by_title(
    connection: NotesConnection,
    user_id: UUID,
    title: str,
    content: str,
) -> UpsertOutcome:
    """Create or fully replace the note with an exact title (tool semantics)."""

    validate_title(title)
    validate_content(content)
    existing = await connection.fetchrow(_RESOLVE_EXACT_SQL, title)
    created = existing is None
    limit = notes_limit()
    if created and await count_notes(connection) >= limit:
        raise NotesLimitReached(limit)
    await connection.execute(
        """
        INSERT INTO notes (user_id, title, content, created_at, updated_at)
        VALUES ($1, $2, $3, now(), now())
        ON CONFLICT (user_id, title)
        DO UPDATE SET content = EXCLUDED.content, updated_at = now()
        """,
        user_id,
        title,
        content,
    )
    return UpsertOutcome(created=created, notes_count=await count_notes(connection))


async def append_to_note_by_title(
    connection: NotesConnection,
    user_id: UUID,
    title: str,
    text: str,
) -> AppendOutcome:
    """Append one entry to a note resolved by title, creating it when missing."""

    validate_title(title)
    existing = await resolve_note(connection, title, for_update=True)
    created = existing is None
    if existing is None:
        limit = notes_limit()
        if await count_notes(connection) >= limit:
            raise NotesLimitReached(limit)
        validate_content(text)
        await connection.execute(
            """
            INSERT INTO notes (user_id, title, content, created_at, updated_at)
            VALUES ($1, $2, $3, now(), now())
            """,
            user_id,
            title,
            text,
        )
        stored_title = title
        note_content = text
    else:
        stored_title = existing.title
        note_content = f"{existing.content}\n{text}" if existing.content else text
        if len(note_content) > MAX_NOTE_CONTENT_LENGTH:
            raise NoteOverflow(stored_title, len(note_content))
        await connection.execute(
            "UPDATE notes SET content = $2, updated_at = now() WHERE title = $1",
            stored_title,
            note_content,
        )
    return AppendOutcome(
        title=stored_title,
        created=created,
        note_chars=len(note_content),
        notes_count=await count_notes(connection),
    )


async def list_notes_for_tool(connection: NotesConnection, limit: int) -> ToolNoteList:
    """List the most recently updated notes, capped to fit the model's context budget."""

    rows = await connection.fetch(
        """
        SELECT title, updated_at, length(content) AS chars
        FROM notes
        ORDER BY updated_at DESC, title
        LIMIT $1
        """,
        limit,
    )
    total = await count_notes(connection)
    return ToolNoteList(
        items=tuple(
            NoteTitleSummary(
                title=_as_str(row["title"]),
                updated_at=_as_datetime(row["updated_at"]),
                chars=int(cast(int, row["chars"])),
            )
            for row in rows
        ),
        total=total,
    )


async def delete_note_by_title(connection: NotesConnection, title: str) -> int | None:
    """Delete a note resolved by title; return the remaining count, or None when missing."""

    validate_title(title)
    note = await resolve_note(connection, title)
    if note is None:
        return None
    await connection.execute("DELETE FROM notes WHERE title = $1", note.title)
    return await count_notes(connection)


async def list_note_summaries(connection: NotesConnection) -> list[NoteSummary]:
    """List every visible note, newest first, without any cap (Mini App)."""

    rows = await connection.fetch(
        """
        SELECT id, title, updated_at, length(content) AS chars
        FROM notes
        ORDER BY updated_at DESC, title
        """
    )
    return [
        NoteSummary(
            id=int(cast(int, row["id"])),
            title=_as_str(row["title"]),
            updated_at=_as_datetime(row["updated_at"]),
            chars=int(cast(int, row["chars"])),
        )
        for row in rows
    ]


async def get_note(
    connection: NotesConnection,
    note_id: int,
    *,
    for_update: bool = False,
) -> NoteRecord | None:
    """Fetch one visible note by id, optionally locking its row."""

    suffix = " FOR UPDATE" if for_update else ""
    row = await connection.fetchrow(
        f"SELECT {_NOTE_COLUMNS} FROM notes WHERE id = $1{suffix}",
        note_id,
    )
    return _record(row) if row is not None else None


async def _title_taken(
    connection: NotesConnection,
    title: str,
    *,
    excluding_id: int | None,
) -> bool:
    row = await connection.fetchrow(
        """
        SELECT id FROM notes
        WHERE lower(btrim(title)) = lower($1) AND ($2::bigint IS NULL OR id <> $2)
        LIMIT 1
        """,
        title,
        excluding_id,
    )
    return row is not None


async def create_note(
    connection: NotesConnection,
    user_id: UUID,
    title: str,
    content: str,
) -> NoteRecord:
    """Create a note; an existing title (any case) is a conflict, never an upsert."""

    validate_title(title)
    validate_content(content)
    if await _title_taken(connection, title, excluding_id=None):
        raise NoteTitleTaken
    limit = notes_limit()
    if await count_notes(connection) >= limit:
        raise NotesLimitReached(limit)
    try:
        row = await connection.fetchrow(
            f"""
            INSERT INTO notes (user_id, title, content, created_at, updated_at)
            VALUES ($1, $2, $3, now(), now())
            RETURNING {_NOTE_COLUMNS}
            """,
            user_id,
            title,
            content,
        )
    except asyncpg.UniqueViolationError as exc:
        raise NoteTitleTaken from exc
    return _record(row)


async def update_note(
    connection: NotesConnection,
    note_id: int,
    *,
    title: str | None,
    content: str | None,
    expected_updated_at: datetime | None,
) -> NoteRecord:
    """Rename and/or rewrite a note with an optional optimistic version check.

    A write that changes neither title nor text leaves updated_at untouched.
    """

    if title is not None:
        validate_title(title)
    if content is not None:
        validate_content(content)
    current = await get_note(connection, note_id, for_update=True)
    if current is None:
        raise NoteNotFound
    if expected_updated_at is not None and expected_updated_at != current.updated_at:
        raise NoteStale(current)
    new_title = current.title if title is None else title
    new_content = current.content if content is None else content
    if new_title == current.title and new_content == current.content:
        return current
    if new_title != current.title and await _title_taken(
        connection, new_title, excluding_id=note_id
    ):
        raise NoteTitleTaken
    try:
        row = await connection.fetchrow(
            f"""
            UPDATE notes SET title = $2, content = $3, updated_at = now()
            WHERE id = $1
            RETURNING {_NOTE_COLUMNS}
            """,
            note_id,
            new_title,
            new_content,
        )
    except asyncpg.UniqueViolationError as exc:
        raise NoteTitleTaken from exc
    return _record(row)


async def delete_note_by_id(connection: NotesConnection, note_id: int) -> bool:
    """Delete one visible note by id; False when it does not exist for this user."""

    result = await connection.execute("DELETE FROM notes WHERE id = $1", note_id)
    return result == "DELETE 1"


def _record(row: Any) -> NoteRecord:
    return NoteRecord(
        id=int(cast(int, row["id"])),
        title=_as_str(row["title"]),
        content=_as_str(row["content"]),
        created_at=_as_datetime(row["created_at"]),
        updated_at=_as_datetime(row["updated_at"]),
    )


def _as_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("note column is not a string")
    return value


def _as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("note timestamp is not a datetime")
    return value
