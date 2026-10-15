"""Offline unit coverage for the shared topic-note domain service."""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from core.notes_service import (
    MAX_NOTE_CONTENT_LENGTH,
    MAX_NOTE_TITLE_LENGTH,
    InvalidNoteTitle,
    NoteContentTooLong,
    NoteNotFound,
    NotesLimitReached,
    NoteStale,
    NoteTitleTaken,
    create_note,
    delete_note_by_id,
    get_note,
    list_note_summaries,
    resolve_note,
    sanitize_title,
    update_note,
    validate_content,
    validate_title,
)
from core.profile import profile_limits

BASE_TIME = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
USER_A = UUID("018f0000-0000-7000-8000-00000000000a")
USER_B = UUID("018f0000-0000-7000-8000-00000000000b")


class FakeNotesStore:
    """In-memory notes table shared by all fake connections (the 'database')."""

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.users: dict[UUID, dict[str, str]] = {}
        self.next_id = 1
        self.clock = 0

    def tick(self) -> datetime:
        self.clock += 1
        return BASE_TIME + timedelta(minutes=self.clock)

    def add(
        self,
        user_id: UUID,
        title: str,
        content: str = "",
        *,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        moment = updated_at or self.tick()
        note = {
            "id": self.next_id,
            "user_id": user_id,
            "title": title,
            "content": content,
            "created_at": moment,
            "updated_at": moment,
        }
        self.next_id += 1
        self.notes.append(note)
        return note


class FakeNotesConnection:
    """Reproduces only the SQL the note service issues, with RLS-like filtering."""

    def __init__(self, store: FakeNotesStore) -> None:
        self.store = store
        self.current_user_id: UUID | None = None
        self.writes = 0

    def _visible(self) -> list[dict[str, Any]]:
        return [note for note in self.store.notes if note["user_id"] == self.current_user_id]

    def _sorted(self) -> list[dict[str, Any]]:
        return sorted(
            self._visible(), key=lambda note: (-note["updated_at"].timestamp(), note["title"])
        )

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            self.current_user_id = UUID(str(args[0]))
            return "SELECT 1"
        if query.startswith("DELETE FROM notes WHERE id"):
            note = next((item for item in self._visible() if item["id"] == args[0]), None)
            if note is None:
                return "DELETE 0"
            self.store.notes.remove(note)
            self.writes += 1
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {query}")

    async def fetchval(self, query: str, *args: object) -> object:
        del args
        if "count(*) FROM notes" in query:
            return len(self._visible())
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        if "FROM users" in query:
            user = self.store.users.get(UUID(str(args[0])))
            return user.copy() if user and UUID(str(args[0])) == self.current_user_id else None
        if "INSERT INTO notes" in query:
            assert args[0] == self.current_user_id
            title, content = str(args[1]), str(args[2])
            if any(note["title"] == title for note in self._visible()):
                raise AssertionError("insert violated the unique title constraint")
            self.writes += 1
            return self.store.add(UUID(str(args[0])), title, content).copy()
        if query.lstrip().startswith("UPDATE notes SET title"):
            note = next((item for item in self._visible() if item["id"] == args[0]), None)
            assert note is not None
            note["title"], note["content"] = str(args[1]), str(args[2])
            note["updated_at"] = self.store.tick()
            self.writes += 1
            return note.copy()
        if "SELECT id, title, content, created_at, updated_at FROM notes" in query:
            note = next((item for item in self._visible() if item["id"] == args[0]), None)
            return note.copy() if note is not None else None
        if "lower(btrim(title)) = lower($1) AND" in query:
            title, excluding = str(args[0]), args[1]
            for note in self._visible():
                if note["title"].strip().lower() == title.lower() and note["id"] != excluding:
                    return {"id": note["id"]}
            return None
        if "lower(btrim(title))" in query:
            matches = [
                note
                for note in self._sorted()
                if note["title"].strip().lower() == str(args[0]).lower()
            ]
            return matches[0].copy() if matches else None
        if "WHERE title = $1" in query:
            return next((n.copy() for n in self._visible() if n["title"] == args[0]), None)
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        del args
        if "SELECT id, title, updated_at, length(content) AS chars" in query:
            return [{**note, "chars": len(note["content"])} for note in self._sorted()]
        raise AssertionError(f"unexpected fetch: {query}")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


def _connection(user_id: UUID = USER_A) -> tuple[FakeNotesConnection, FakeNotesStore]:
    store = FakeNotesStore()
    connection = FakeNotesConnection(store)
    connection.current_user_id = user_id
    return connection, store


def test_sanitize_title_trims_and_collapses_whitespace() -> None:
    assert sanitize_title("  Идеи \t  подарков\n") == "Идеи подарков"
    assert sanitize_title("   ") == ""


def test_title_validation_bounds() -> None:
    validate_title("a" * MAX_NOTE_TITLE_LENGTH)
    with pytest.raises(InvalidNoteTitle):
        validate_title("")
    with pytest.raises(InvalidNoteTitle):
        validate_title("a" * (MAX_NOTE_TITLE_LENGTH + 1))


def test_content_validation_bounds() -> None:
    validate_content("")
    validate_content("x" * MAX_NOTE_CONTENT_LENGTH)
    with pytest.raises(NoteContentTooLong):
        validate_content("x" * (MAX_NOTE_CONTENT_LENGTH + 1))


async def test_resolve_prefers_exact_then_case_insensitive() -> None:
    connection, store = _connection()
    store.add(USER_A, "Пробежки", "a")
    store.add(USER_A, "пробежки", "b")

    exact = await resolve_note(connection, "пробежки")
    fallback = await resolve_note(connection, "ПРОБЕЖКИ")
    missing = await resolve_note(connection, "Другое")

    assert exact is not None and exact.content == "b"
    assert fallback is not None and fallback.content == "b"  # newest wins
    assert missing is None


async def test_list_has_no_cap_and_orders_by_updated_then_title() -> None:
    connection, store = _connection()
    for index in range(120):
        store.add(USER_A, f"Тема {index:03d}", "x", updated_at=BASE_TIME + timedelta(minutes=index))
    store.add(USER_A, "Б", "", updated_at=BASE_TIME + timedelta(days=1))
    store.add(USER_A, "А", "", updated_at=BASE_TIME + timedelta(days=1))
    store.add(USER_B, "Чужая", "secret")

    items = await list_note_summaries(connection)

    assert len(items) == 122
    assert [item.title for item in items[:2]] == ["А", "Б"]
    assert items[0].updated_at >= items[1].updated_at >= items[2].updated_at
    assert all(item.title != "Чужая" for item in items)


async def test_create_rejects_case_variant_of_existing_title() -> None:
    connection, store = _connection()
    store.add(USER_A, "Пробежки")

    with pytest.raises(NoteTitleTaken):
        await create_note(connection, USER_A, "пробежки", "")
    created = await create_note(connection, USER_A, "Забеги", "")
    assert created.title == "Забеги"


async def test_create_enforces_note_limit_and_length_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_NOTES_PER_USER", "2")
    assert profile_limits().notes_per_user == 2
    connection, _store = _connection()

    exact = await create_note(connection, USER_A, "a" * 64, "x" * MAX_NOTE_CONTENT_LENGTH)
    assert len(exact.content) == MAX_NOTE_CONTENT_LENGTH
    with pytest.raises(NoteContentTooLong):
        await create_note(connection, USER_A, "Вторая", "x" * (MAX_NOTE_CONTENT_LENGTH + 1))
    with pytest.raises(InvalidNoteTitle):
        await create_note(connection, USER_A, "a" * 65, "")
    await create_note(connection, USER_A, "Вторая", "")
    with pytest.raises(NotesLimitReached) as limit:
        await create_note(connection, USER_A, "Третья", "")
    assert limit.value.limit == 2


async def test_update_detects_stale_version_and_returns_current_note() -> None:
    connection, store = _connection()
    note = store.add(USER_A, "Тема", "старое")
    seen = note["updated_at"]
    note["content"] = "дописал ассистент"
    note["updated_at"] = store.tick()

    with pytest.raises(NoteStale) as stale:
        await update_note(
            connection, note["id"], title=None, content="моя правка", expected_updated_at=seen
        )
    assert stale.value.current.content == "дописал ассистент"
    assert note["content"] == "дописал ассистент"

    forced = await update_note(
        connection, note["id"], title=None, content="моя правка", expected_updated_at=None
    )
    assert forced.content == "моя правка"


async def test_update_rename_conflicts_but_case_only_change_of_own_title_is_fine() -> None:
    connection, store = _connection()
    first = store.add(USER_A, "Пробежки")
    store.add(USER_A, "Забеги")

    with pytest.raises(NoteTitleTaken):
        await update_note(
            connection, first["id"], title="забеги", content=None, expected_updated_at=None
        )
    renamed = await update_note(
        connection, first["id"], title="ПРОБЕЖКИ", content=None, expected_updated_at=None
    )
    assert renamed.title == "ПРОБЕЖКИ"


async def test_update_without_actual_change_does_not_write_or_move_updated_at() -> None:
    connection, store = _connection()
    note = store.add(USER_A, "Тема", "текст")
    before = note["updated_at"]

    result = await update_note(
        connection, note["id"], title="Тема", content="текст", expected_updated_at=before
    )

    assert result.updated_at == before
    assert connection.writes == 0


async def test_update_and_delete_of_foreign_or_missing_note_look_missing() -> None:
    connection, store = _connection(USER_A)
    foreign = store.add(USER_B, "Чужая", "secret")

    with pytest.raises(NoteNotFound):
        await update_note(
            connection, foreign["id"], title="x", content=None, expected_updated_at=None
        )
    assert await get_note(connection, foreign["id"]) is None
    assert await delete_note_by_id(connection, foreign["id"]) is False
    assert any(note["id"] == foreign["id"] for note in store.notes)
