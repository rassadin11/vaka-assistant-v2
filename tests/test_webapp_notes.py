"""HTTP coverage for Mini App notes, RLS boundaries, and optimistic locking."""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from prometheus_client import CollectorRegistry

from tests.test_notes_service import BASE_TIME, FakeNotesConnection, FakeNotesStore
from webapp.app import create_app
from webapp.auth import create_session_token
from webapp.metrics import WebAppMetrics
from webapp.settings import WebAppSettings

SESSION_SECRET = "notes-test-session-secret"


class NotesPool:
    def __init__(self, store: FakeNotesStore) -> None:
        self.connection = FakeNotesConnection(store)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeNotesConnection]:
        yield self.connection


class AllowRedis:
    async def eval(self, _script: str, _numkeys: int, *_args: object) -> object:
        return [1, 0]

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _client(store: FakeNotesStore, *user_ids: UUID) -> tuple[httpx.AsyncClient, WebAppMetrics]:
    for user_id in user_ids:
        store.users[user_id] = {"status": "active", "timezone": "Europe/Moscow", "plan": "trial"}
    metrics = WebAppMetrics(CollectorRegistry())
    app = create_app(
        settings=WebAppSettings(
            telegram_bot_token="notes-test-bot",
            session_secret=SESSION_SECRET,
            database_url="postgresql://unused",
            redis_cache_url="redis://unused",
        ),
        pool=NotesPool(store),  # type: ignore[arg-type]
        cache_redis=AllowRedis(),  # type: ignore[arg-type]
        metrics=metrics,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://notes.test"
    ), metrics


def _auth(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_session_token(user_id, SESSION_SECRET)}"}


def _note(store: FakeNotesStore, user_id: UUID, title: str, content: str = "") -> dict[str, Any]:
    return store.add(user_id, title, content)


async def test_endpoints_require_auth() -> None:
    user_id = uuid4()
    client, _metrics = _client(FakeNotesStore(), user_id)
    async with client:
        responses = [
            await client.get("/app/api/notes"),
            await client.get("/app/api/notes/1"),
            await client.post("/app/api/notes", json={"title": "x"}),
            await client.patch("/app/api/notes/1", json={"title": "x"}),
            await client.delete("/app/api/notes/1"),
        ]

    assert [response.status_code for response in responses] == [401] * 5


async def test_list_returns_all_own_notes_in_user_timezone_with_limit() -> None:
    user_id, other = uuid4(), uuid4()
    store = FakeNotesStore()
    for index in range(60):
        store.add(
            user_id,
            f"Тема {index:02d}",
            "x" * index,
            updated_at=BASE_TIME + timedelta(minutes=index),
        )
    _note(store, other, "Чужая", "secret")
    client, _metrics = _client(store, user_id, other)
    async with client:
        response = await client.get("/app/api/notes", headers=_auth(user_id))

    payload = response.json()
    assert response.status_code == 200
    assert payload["notes_total"] == 60 and len(payload["notes"]) == 60
    assert payload["notes_limit"] == 100
    first = payload["notes"][0]
    assert first["title"] == "Тема 59" and first["chars"] == 59
    assert first["updated_at"] == "2026-10-01T12:59:00+03:00"
    assert all(item["title"] != "Чужая" for item in payload["notes"])


async def test_get_returns_full_note_and_hides_foreign_one() -> None:
    user_a, user_b = uuid4(), uuid4()
    store = FakeNotesStore()
    own = _note(store, user_a, "Моя", "строка 1\nстрока 2")
    foreign = _note(store, user_b, "Чужая", "secret")
    client, _metrics = _client(store, user_a, user_b)
    async with client:
        ok = await client.get(f"/app/api/notes/{own['id']}", headers=_auth(user_a))
        hidden = await client.get(f"/app/api/notes/{foreign['id']}", headers=_auth(user_a))
        missing = await client.get("/app/api/notes/999", headers=_auth(user_a))

    assert ok.json()["content"] == "строка 1\nстрока 2" and ok.json()["chars"] == 17
    assert ok.json()["created_at"].endswith("+03:00")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["error"]["code"] == "note_not_found"
    assert "secret" not in hidden.text


async def test_create_validates_and_rejects_duplicates_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_NOTES_PER_USER", "3")
    user_id = uuid4()
    store = FakeNotesStore()
    client, metrics = _client(store, user_id)
    headers = _auth(user_id)
    async with client:
        created = await client.post(
            "/app/api/notes", json={"title": "  Пробежки   2026 "}, headers=headers
        )
        blank = await client.post("/app/api/notes", json={"title": "   "}, headers=headers)
        long_title = await client.post("/app/api/notes", json={"title": "a" * 65}, headers=headers)
        exact_text = await client.post(
            "/app/api/notes", json={"title": "Большая", "content": "x" * 8000}, headers=headers
        )
        long_text = await client.post(
            "/app/api/notes", json={"title": "Огромная", "content": "x" * 8001}, headers=headers
        )
        taken = await client.post(
            "/app/api/notes", json={"title": "пробежки 2026"}, headers=headers
        )
        await client.post("/app/api/notes", json={"title": "Третья"}, headers=headers)
        over_limit = await client.post(
            "/app/api/notes", json={"title": "Четвёртая"}, headers=headers
        )

    assert created.status_code == 201
    assert created.json()["title"] == "Пробежки 2026" and created.json()["content"] == ""
    assert blank.status_code == 400 and blank.json()["error"]["code"] == "invalid_title"
    assert long_title.status_code == 400
    assert exact_text.status_code == 201
    assert long_text.status_code == 400 and long_text.json()["error"]["code"] == "content_too_long"
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "title_taken"
    assert over_limit.status_code == 409 and over_limit.json()["error"]["code"] == "notes_limit"
    assert len(store.notes) == 3
    assert metrics.notes_created._value.get() == 3


async def test_a_full_length_cyrillic_note_fits_the_request_body_cap() -> None:
    """8000 characters is legal, and Cyrillic costs two UTF-8 bytes per character.

    The body cap used to be sized for the short calendar and finance payloads, so
    a note of a perfectly legal length was rejected with 413 before any domain
    validation ran. The character limit belongs to the domain: 8001 is a 400.
    """

    user_id = uuid4()
    store = FakeNotesStore()
    client, _metrics = _client(store, user_id)
    async with client:
        at_limit = await client.post(
            "/app/api/notes",
            headers=_auth(user_id),
            json={"title": "Ровно предел", "content": "я" * 8000},
        )
        over_limit = await client.post(
            "/app/api/notes",
            headers=_auth(user_id),
            json={"title": "На символ больше", "content": "я" * 8001},
        )

    assert at_limit.status_code == 201, at_limit.json()
    assert at_limit.json()["chars"] == 8000
    assert over_limit.status_code == 400
    assert over_limit.json()["error"]["code"] == "content_too_long"


async def test_patch_renames_updates_and_counts_metric() -> None:
    user_id = uuid4()
    store = FakeNotesStore()
    note = _note(store, user_id, "Старое", "текст")
    client, metrics = _client(store, user_id)
    async with client:
        response = await client.patch(
            f"/app/api/notes/{note['id']}",
            json={"title": "Новое", "content": "новый\nтекст"},
            headers=_auth(user_id),
        )

    assert response.status_code == 200
    assert response.json()["title"] == "Новое" and response.json()["content"] == "новый\nтекст"
    assert store.notes[0]["title"] == "Новое"
    assert metrics.notes_updated._value.get() == 1


async def test_patch_requires_a_field_and_rejects_bad_values() -> None:
    user_id = uuid4()
    store = FakeNotesStore()
    note = _note(store, user_id, "Тема", "текст")
    client, _metrics = _client(store, user_id)
    url = f"/app/api/notes/{note['id']}"
    async with client:
        empty = await client.patch(url, json={}, headers=_auth(user_id))
        blank = await client.patch(url, json={"title": "  "}, headers=_auth(user_id))
        long_text = await client.patch(url, json={"content": "x" * 8001}, headers=_auth(user_id))
        naive = await client.patch(
            url,
            json={"content": "y", "expected_updated_at": "2026-10-01T10:00:00"},
            headers=_auth(user_id),
        )

    assert empty.status_code == 400 and empty.json()["error"]["code"] == "nothing_to_update"
    assert blank.status_code == 400
    assert long_text.status_code == 400
    assert naive.status_code == 422
    assert store.notes[0]["content"] == "текст"


async def test_patch_rename_into_existing_title_conflicts_and_keeps_both() -> None:
    user_id = uuid4()
    store = FakeNotesStore()
    first = _note(store, user_id, "Пробежки", "a")
    _note(store, user_id, "Забеги", "b")
    client, metrics = _client(store, user_id)
    async with client:
        conflict = await client.patch(
            f"/app/api/notes/{first['id']}", json={"title": "забеги"}, headers=_auth(user_id)
        )
        case_only = await client.patch(
            f"/app/api/notes/{first['id']}", json={"title": "ПРОБЕЖКИ"}, headers=_auth(user_id)
        )

    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "title_taken"
    assert case_only.status_code == 200
    assert sorted(note["title"] for note in store.notes) == ["Забеги", "ПРОБЕЖКИ"]
    assert metrics.notes_updated._value.get() == 1


async def test_patch_stale_returns_409_with_current_note_and_force_overwrites() -> None:
    user_id = uuid4()
    store = FakeNotesStore()
    note = _note(store, user_id, "Тема", "старое")
    client, metrics = _client(store, user_id)
    url = f"/app/api/notes/{note['id']}"
    async with client:
        opened = (await client.get(url, headers=_auth(user_id))).json()
        note["content"] += "\nдописал ассистент"
        note["updated_at"] = store.tick()
        stale = await client.patch(
            url,
            json={"content": "моя правка", "expected_updated_at": opened["updated_at"]},
            headers=_auth(user_id),
        )
        current = (await client.get(url, headers=_auth(user_id))).json()
        matching = await client.patch(
            url,
            json={"content": "после обновления", "expected_updated_at": current["updated_at"]},
            headers=_auth(user_id),
        )
        forced = await client.patch(url, json={"content": "всё равно"}, headers=_auth(user_id))

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale"
    assert stale.json()["note"]["content"] == "старое\nдописал ассистент"
    assert stale.json()["note"]["updated_at"] == current["updated_at"]
    assert matching.status_code == 200
    assert forced.status_code == 200 and store.notes[0]["content"] == "всё равно"
    assert metrics.notes_updated._value.get() == 2


async def test_patch_without_actual_change_keeps_updated_at() -> None:
    user_id = uuid4()
    store = FakeNotesStore()
    note = _note(store, user_id, "Тема", "текст")
    before = note["updated_at"]
    client, _metrics = _client(store, user_id)
    async with client:
        response = await client.patch(
            f"/app/api/notes/{note['id']}",
            json={"title": "Тема", "content": "текст"},
            headers=_auth(user_id),
        )

    assert response.status_code == 200
    assert note["updated_at"] == before
    assert datetime.fromisoformat(response.json()["updated_at"]).astimezone(UTC) == before


async def test_patch_and_delete_are_rls_hidden_and_delete_is_idempotent_404() -> None:
    user_a, user_b = uuid4(), uuid4()
    store = FakeNotesStore()
    own = _note(store, user_a, "Моя")
    foreign = _note(store, user_b, "Чужая", "secret")
    client, metrics = _client(store, user_a, user_b)
    async with client:
        patch = await client.patch(
            f"/app/api/notes/{foreign['id']}", json={"content": "взлом"}, headers=_auth(user_a)
        )
        delete_foreign = await client.delete(
            f"/app/api/notes/{foreign['id']}", headers=_auth(user_a)
        )
        deleted = await client.delete(f"/app/api/notes/{own['id']}", headers=_auth(user_a))
        again = await client.delete(f"/app/api/notes/{own['id']}", headers=_auth(user_a))

    assert patch.status_code == delete_foreign.status_code == again.status_code == 404
    assert deleted.status_code == 204 and deleted.content == b""
    assert [note["title"] for note in store.notes] == ["Чужая"]
    assert store.notes[0]["content"] == "secret"
    assert metrics.notes_deleted._value.get() == 1
