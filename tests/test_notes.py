"""Offline coverage for the topic note tools and their registration."""

# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from core.context import TaskContext
from core.tools import RiskLevel, ToolRegistry
from tests.test_tools import FakePool as RegistryFakePool
from tests.test_tools import FakeRedis
from tools.notes import (
    MAX_NOTE_CONTENT_LENGTH,
    MAX_NOTES_PER_USER,
    AppendToNoteArgs,
    DeleteNoteArgs,
    ReadNoteArgs,
    UpsertNoteArgs,
    _append_to_note,
    _delete_note,
    _list_notes,
    _read_note,
    _upsert_note,
    register_notes_tools,
)

USER_ID = UUID("018f0000-0000-7000-8000-000000000001")
BASE_TIME = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakeNotesConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeNotesConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeNotesConnection:
    """In-memory stand-in reproducing only the SQL the note tools issue."""

    def __init__(self, notes: list[dict[str, Any]] | None = None) -> None:
        self.notes: list[dict[str, Any]] = notes or []
        self._clock = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    def _now(self) -> datetime:
        self._clock += 1
        return BASE_TIME + timedelta(minutes=self._clock)

    def _by_title(self, title: str) -> dict[str, Any] | None:
        for note in self.notes:
            if note["title"] == title:
                return note
        return None

    def _sorted(self) -> list[dict[str, Any]]:
        return sorted(
            self.notes,
            key=lambda note: (note["updated_at"], note["title"]),
            reverse=True,
        )

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            return "SELECT 1"
        if "INSERT INTO notes" in query:
            title, content = str(args[1]), str(args[2])
            existing = self._by_title(title)
            if existing is not None and "ON CONFLICT" in query:
                existing["content"] = content
                existing["updated_at"] = self._now()
                return "INSERT 0 1"
            if existing is not None:
                raise AssertionError("plain insert violated the unique title constraint")
            moment = self._now()
            self.notes.append(
                {"title": title, "content": content, "created_at": moment, "updated_at": moment}
            )
            return "INSERT 0 1"
        if "UPDATE notes SET content" in query:
            note = self._by_title(str(args[0]))
            assert note is not None
            note["content"] = str(args[1])
            note["updated_at"] = self._now()
            return "UPDATE 1"
        if "DELETE FROM notes" in query:
            note = self._by_title(str(args[0]))
            assert note is not None
            self.notes.remove(note)
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {query}")

    async def fetchval(self, query: str, *args: object) -> object:
        del args
        if "count(*) FROM notes" in query:
            return len(self.notes)
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        title = str(args[0])
        if "lower(btrim(title))" in query:
            matches = [
                note for note in self._sorted() if note["title"].strip().lower() == title.lower()
            ]
            return matches[0] if matches else None
        if "WHERE title = $1" in query:
            return self._by_title(title)
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        if "length(content) AS chars" in query:
            return [
                {
                    "title": note["title"],
                    "updated_at": note["updated_at"],
                    "chars": len(note["content"]),
                }
                for note in self._sorted()
            ]
        if "SELECT title" in query:
            limit = int(args[0])
            return [{"title": note["title"]} for note in self._sorted()[:limit]]
        raise AssertionError(f"unexpected fetch: {query}")


class FakePool:
    def __init__(self, notes: list[dict[str, Any]] | None = None) -> None:
        self.connection = FakeNotesConnection(notes)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _context(timezone: str = "Europe/Moscow") -> TaskContext:
    return TaskContext(
        user_id=USER_ID,
        tg_user_id=101,
        chat_id=101,
        update_id=1,
        timezone=timezone,
        plan="trial",
        trace_id=UUID("018f0000-0000-7000-8000-0000000000ff"),
    )


def _filled_pool(count: int) -> FakePool:
    pool = FakePool()
    for index in range(count):
        pool.connection.notes.append(
            {
                "title": f"Тема {index}",
                "content": "запись",
                "created_at": BASE_TIME,
                "updated_at": BASE_TIME + timedelta(minutes=index),
            }
        )
    return pool


async def test_upsert_creates_then_fully_replaces_a_note() -> None:
    pool = FakePool()

    created = await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="Пробежки", content="2026-07-30: 5 км"),
    )
    replaced = await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="Пробежки", content="начинаю заново"),
    )

    assert created.payload == {"created": True, "notes_count": 1, "notes_limit": 100}
    assert replaced.payload == {"created": False, "notes_count": 1, "notes_limit": 100}
    assert pool.connection.notes[0]["content"] == "начинаю заново"


async def test_append_adds_a_line_to_an_existing_note() -> None:
    pool = FakePool()
    await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="Пробежки", content="2026-07-30: 5 км"),
    )

    result = await _append_to_note(
        pool,  # type: ignore[arg-type]
        _context(),
        AppendToNoteArgs(title="Пробежки", text="2026-07-31: 5 км за 26:30"),
    )

    assert result.status == "ok"
    assert result.payload["created"] is False
    assert result.payload["note_chars"] == len(pool.connection.notes[0]["content"])
    assert result.payload["note_chars_limit"] == MAX_NOTE_CONTENT_LENGTH
    assert pool.connection.notes[0]["content"] == "2026-07-30: 5 км\n2026-07-31: 5 км за 26:30"


async def test_append_creates_a_missing_note_without_a_leading_newline() -> None:
    pool = FakePool()

    result = await _append_to_note(
        pool,  # type: ignore[arg-type]
        _context(),
        AppendToNoteArgs(title="  Идеи\nподарков  ", text="набор для пайки"),
    )

    assert result.payload["created"] is True
    assert result.payload["notes_count"] == 1
    assert pool.connection.notes[0]["title"] == "Идеи подарков"
    assert pool.connection.notes[0]["content"] == "набор для пайки"


async def test_append_resolves_an_existing_title_case_insensitively() -> None:
    pool = FakePool()
    await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="Пробежки", content="2026-07-30: 5 км"),
    )

    result = await _append_to_note(
        pool,  # type: ignore[arg-type]
        _context(),
        AppendToNoteArgs(title="пробежки", text="2026-07-31: 6 км"),
    )

    assert result.payload["created"] is False
    assert len(pool.connection.notes) == 1
    assert pool.connection.notes[0]["title"] == "Пробежки"


async def test_upsert_refuses_a_new_note_beyond_the_limit_but_keeps_replacing() -> None:
    pool = _filled_pool(MAX_NOTES_PER_USER)

    refused = await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="Сто первая", content="текст"),
    )
    replaced = await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="Тема 0", content="переписано"),
    )

    assert refused.status == "error"
    assert refused.retryable is False
    assert "Достигнут лимит 100 заметок" in (refused.error or "")
    assert "list_notes" in (refused.error or "")
    assert len(pool.connection.notes) == MAX_NOTES_PER_USER
    assert replaced.status == "ok"


async def test_append_refuses_a_new_note_beyond_the_limit_but_keeps_appending() -> None:
    pool = _filled_pool(MAX_NOTES_PER_USER)

    refused = await _append_to_note(
        pool,  # type: ignore[arg-type]
        _context(),
        AppendToNoteArgs(title="Сто первая", text="запись"),
    )
    appended = await _append_to_note(
        pool,  # type: ignore[arg-type]
        _context(),
        AppendToNoteArgs(title="Тема 0", text="ещё запись"),
    )

    assert refused.status == "error"
    assert refused.retryable is False
    assert "Достигнут лимит 100 заметок" in (refused.error or "")
    assert len(pool.connection.notes) == MAX_NOTES_PER_USER
    assert appended.status == "ok"


async def test_append_reports_the_actual_size_when_the_note_overflows() -> None:
    pool = FakePool(
        [
            {
                "title": "Пробежки",
                "content": "a" * (MAX_NOTE_CONTENT_LENGTH - 5),
                "created_at": BASE_TIME,
                "updated_at": BASE_TIME,
            }
        ]
    )

    result = await _append_to_note(
        pool,  # type: ignore[arg-type]
        _context(),
        AppendToNoteArgs(title="Пробежки", text="b" * 20),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert result.error == (
        'Заметка "Пробежки" переполнена (8016/8000 символов). '
        "Предложите пользователю ревизию: сократить её, разбить на части или удалить лишнее."
    )
    assert pool.connection.notes[0]["content"] == "a" * (MAX_NOTE_CONTENT_LENGTH - 5)


async def test_title_is_sanitized_and_bounded() -> None:
    pool = FakePool()

    too_long = await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="я" * 65, content="текст"),
    )
    empty = await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="   ", content="текст"),
    )

    assert too_long.status == "error"
    assert too_long.retryable is True
    assert "64" in (too_long.error or "")
    assert empty.status == "error"
    assert empty.retryable is True
    assert pool.connection.notes == []


async def test_upsert_rejects_content_over_the_hard_limit() -> None:
    pool = FakePool()

    result = await _upsert_note(
        pool,  # type: ignore[arg-type]
        _context(),
        UpsertNoteArgs(title="Пробежки", content="a" * (MAX_NOTE_CONTENT_LENGTH + 1)),
    )

    assert result.status == "error"
    assert result.retryable is True
    assert pool.connection.notes == []


def test_append_text_length_is_enforced_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        AppendToNoteArgs(title="Пробежки", text="a" * 1001)


async def test_read_resolves_case_insensitively_and_suggests_titles_when_missing() -> None:
    pool = _filled_pool(12)

    found = await _read_note(
        pool,  # type: ignore[arg-type]
        _context(),
        ReadNoteArgs(title="тема 3"),
    )
    missing = await _read_note(
        pool,  # type: ignore[arg-type]
        _context(),
        ReadNoteArgs(title="Велосипед"),
    )

    assert found.status == "ok"
    assert found.payload["title"] == "Тема 3"
    assert found.payload["content"] == "запись"
    assert found.payload["chars"] == len("запись")
    assert missing.status == "error"
    assert missing.retryable is True
    assert (missing.error or "").count('"') == 2 + 2 * 10
    assert '"Тема 11"' in (missing.error or "")
    assert '"Тема 1"' not in (missing.error or "")


async def test_read_is_not_retryable_when_the_user_has_no_notes() -> None:
    pool = FakePool()

    result = await _read_note(
        pool,  # type: ignore[arg-type]
        _context(),
        ReadNoteArgs(title="Пробежки"),
    )

    assert result.status == "error"
    assert result.retryable is False
    assert "нет заметок" in (result.error or "")


async def test_delete_removes_a_case_insensitively_resolved_note() -> None:
    pool = _filled_pool(3)

    deleted = await _delete_note(
        pool,  # type: ignore[arg-type]
        _context(),
        DeleteNoteArgs(title="тема 1"),
    )
    missing = await _delete_note(
        pool,  # type: ignore[arg-type]
        _context(),
        DeleteNoteArgs(title="тема 1"),
    )

    assert deleted.payload == {"notes_count": 2}
    assert [note["title"] for note in pool.connection.notes] == ["Тема 0", "Тема 2"]
    assert missing.status == "error"
    assert missing.retryable is True


async def test_list_returns_titles_sizes_and_local_times_newest_first() -> None:
    pool = _filled_pool(3)

    result = await _list_notes(pool, _context("Asia/Yekaterinburg"))  # type: ignore[arg-type]

    assert result.payload["notes_count"] == 3
    assert result.payload["notes_limit"] == MAX_NOTES_PER_USER
    notes = result.payload["notes"]
    assert [note["title"] for note in notes] == ["Тема 2", "Тема 1", "Тема 0"]
    assert notes[0]["chars"] == len("запись")
    assert notes[0]["updated_at"] == "2026-07-31T14:02:00+05:00"


def test_note_tools_are_registered_with_their_declared_risks_and_limits() -> None:
    registry = ToolRegistry(FakeRedis(), RegistryFakePool())  # type: ignore[arg-type]

    register_notes_tools(registry, FakePool())  # type: ignore[arg-type]

    expected = {
        "upsert_note": (RiskLevel.MUTATING_INTERNAL, 30),
        "append_to_note": (RiskLevel.MUTATING_INTERNAL, 60),
        "list_notes": (RiskLevel.READ_ONLY, None),
        "read_note": (RiskLevel.READ_ONLY, None),
        "delete_note": (RiskLevel.MUTATING_INTERNAL, 30),
    }
    for name, (risk, daily_limit) in expected.items():
        spec = registry.get(name)
        assert spec is not None, name
        assert spec.risk == risk
        assert spec.daily_limit == daily_limit
        assert "user_id" not in spec.to_llm_definition().parameters.get("properties", {})


async def test_replayed_append_is_executed_only_once_by_the_dispatcher() -> None:
    notes_pool = FakePool()
    registry = ToolRegistry(FakeRedis(), RegistryFakePool())  # type: ignore[arg-type]
    register_notes_tools(registry, notes_pool)  # type: ignore[arg-type]
    context = _context()
    arguments = {"title": "Пробежки", "text": "2026-07-31: 5 км"}

    first = await registry.dispatch(context, "append_to_note", arguments, 1)
    replayed = await registry.dispatch(context, "append_to_note", arguments, 1)

    assert first.status == replayed.status == "ok"
    assert first.payload == replayed.payload
    assert notes_pool.connection.notes[0]["content"] == "2026-07-31: 5 км"
