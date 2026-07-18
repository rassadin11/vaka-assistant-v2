"""Unit tests for closed-beta onboarding behavior."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from worker.onboarding import (
    ALREADY_ACTIVE_TEXT,
    ASSISTANT_CAPABILITIES_TEXT,
    HELP_TEXT,
    REJECTED_TEXT,
    START_TIMEZONE_PROMPT_TEXT,
    TIMEZONE_BUTTONS,
    UNKNOWN_CITY_TEXT,
    WELCOME_CTA_TEXT,
    WELCOME_TEXT,
    OnboardingProcessor,
)
from worker.processor import EchoProcessor


def _envelope(
    *,
    update_id: int = 1,
    tg_user_id: int = 100,
    chat_id: int | None = None,
    text: str = "/start",
    kind: str = "text",
    payload: dict[str, Any] | None = None,
) -> UpdateEnvelope:
    return UpdateEnvelope.model_validate(
        {
            "update_id": update_id,
            "user_id": tg_user_id,
            "chat_id": tg_user_id if chat_id is None else chat_id,
            "kind": kind,
            "payload": {"text": text} if payload is None else payload,
        }
    )


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.connection = FakeConnection(self)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)

    def add_user(
        self,
        tg_user_id: int,
        *,
        tg_chat_id: int | None = None,
        status: str = "pending",
        timezone: str = "Europe/Moscow",
        first_name: str | None = None,
        username: str | None = None,
        assistant_profile: dict[str, str] | None = None,
    ) -> None:
        self.users[tg_user_id] = {
            "id": UUID("018f0000-0000-7000-8000-000000000001"),
            "tg_user_id": tg_user_id,
            "tg_chat_id": tg_user_id if tg_chat_id is None else tg_chat_id,
            "username": username,
            "first_name": first_name,
            "status": status,
            "timezone": timezone,
            "plan": "trial",
            "assistant_profile": assistant_profile,
            "created_at": datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
        }


class FakeConnection:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        tg_user_id = int(args[0])
        row = self._pool.users.get(tg_user_id)
        if query.lstrip().startswith("UPDATE users"):
            if row is None:
                return None
            if "status = 'rejected'" in query:
                row["status"] = "rejected"
            elif "SET timezone" in query:
                row["timezone"] = args[1]
            row["updated_at"] = datetime.now(UTC)
            return {"tg_chat_id": row["tg_chat_id"]}
        if row is None:
            return None
        return {
            "id": row["id"],
            "tg_user_id": row["tg_user_id"],
            "tg_chat_id": row["tg_chat_id"],
            "status": row["status"],
            "timezone": row["timezone"],
            "plan": row["plan"],
            "assistant_profile": (
                None
                if row["assistant_profile"] is None
                else json.dumps(row["assistant_profile"], ensure_ascii=False)
            ),
        }

    async def execute(self, query: str, *args: object) -> str:
        if query.lstrip().startswith("INSERT INTO users"):
            user_id, tg_user_id, tg_chat_id, timezone = args
            self._pool.users[int(tg_user_id)] = {
                "id": user_id,
                "tg_user_id": tg_user_id,
                "tg_chat_id": tg_chat_id,
                "username": None,
                "first_name": None,
                "status": "active",
                "timezone": timezone,
                "plan": "trial",
                "assistant_profile": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            return "INSERT 0 1"
        if "status = 'active'" in query:
            tg_user_id = int(args[0])
            self._pool.users[tg_user_id]["status"] = "active"
            self._pool.users[tg_user_id]["updated_at"] = datetime.now(UTC)
            return "UPDATE 1"
        raise AssertionError(f"unexpected query: {query}")


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None, bool]] = []
        self.deleted: list[str] = []

    async def exists(self, name: str) -> int:
        return int(name in self.values)

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> object:
        self.set_calls.append((name, value, ex, nx))
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            self.deleted.append(name)
            if name in self.values:
                deleted += 1
                del self.values[name]
        return deleted


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, list[list[tuple[str, str]]] | None]] = []
        self.callbacks: list[str] = []
        self.admin: list[str] = []
        self.notify_admin_error: Exception | None = None

    async def send(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[tuple[str, str]]] | None = None,
    ) -> None:
        self.sent.append((chat_id, text, buttons))

    async def answer_callback(self, callback_query_id: str) -> None:
        self.callbacks.append(callback_query_id)

    async def notify_admin(self, text: str) -> None:
        self.admin.append(text)
        if self.notify_admin_error is not None:
            raise self.notify_admin_error


class ContextRecorder:
    def __init__(self) -> None:
        self.context: TaskContext | None = None

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        del envelope
        self.context = context
        return "received"


def _processor(
    pool: FakePool,
    cache: FakeCache,
    recorder: Recorder,
    *,
    admin_ids: tuple[int, ...] = (900,),
) -> OnboardingProcessor:
    return OnboardingProcessor(
        service_pool=pool,  # type: ignore[arg-type]
        cache_redis=cache,
        inner=EchoProcessor(),
        send=recorder.send,
        answer_callback=recorder.answer_callback,
        notify_admin=recorder.notify_admin,
        admin_ids=admin_ids,
    )


async def test_first_contact_creates_active_user_and_prompts_for_timezone() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)

    reply = await processor.process(_envelope(tg_user_id=101, chat_id=501, text="hello"))

    assert reply is None
    assert pool.users[101]["status"] == "active"
    assert pool.users[101]["tg_chat_id"] == 501
    assert pool.users[101]["timezone"] == "Europe/Moscow"
    assert cache.values["onboarding:tz_pending:101"] == "1"
    assert recorder.sent == [(501, START_TIMEZONE_PROMPT_TEXT, TIMEZONE_BUTTONS)]
    assert recorder.admin == ["Новый пользователь: id 101."]


async def test_legacy_pending_user_is_activated_and_prompted_on_contact() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="pending")

    assert await processor.process(_envelope(tg_user_id=101)) is None
    assert pool.users[101]["status"] == "active"
    assert cache.values["onboarding:tz_pending:101"] == "1"
    assert recorder.sent == [(101, START_TIMEZONE_PROMPT_TEXT, TIMEZONE_BUTTONS)]


async def test_rejected_and_banned_status_replies() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(102, status="rejected")
    pool.add_user(103, status="banned")

    assert await processor.process(_envelope(tg_user_id=102)) == REJECTED_TEXT
    assert await processor.process(_envelope(update_id=2, tg_user_id=102)) is None
    assert await processor.process(_envelope(tg_user_id=103)) is None


async def test_admin_reject_command_still_rejects_active_user() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(102, tg_chat_id=502, status="active")

    rejected = await processor.process(_envelope(tg_user_id=900, text="/reject 102"))

    assert rejected == "Пользователь 102 отклонён."
    assert pool.users[102]["status"] == "rejected"
    assert recorder.sent == [(502, REJECTED_TEXT, None)]


async def test_admin_malformed_command_returns_usage() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)

    reply = await processor.process(_envelope(tg_user_id=900, text="/reject nope"))

    assert reply == "Использование: /reject <tg_user_id>."


async def test_timezone_callback_updates_timezone_sends_welcome_and_answers_callback() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active")
    cache.values["onboarding:tz_pending:101"] = "1"

    reply = await processor.process(
        _envelope(
            tg_user_id=101,
            kind="callback",
            payload={
                "data": "tz:Asia/Almaty",
                "message_id": 10,
                "callback_query_id": "cb1",
            },
        )
    )

    assert recorder.callbacks == ["cb1"]
    assert pool.users[101]["timezone"] == "Asia/Almaty"
    assert "onboarding:tz_pending:101" in cache.deleted
    assert reply is None
    assert recorder.sent == [
        (101, WELCOME_TEXT.format(tz="Asia/Almaty"), None),
        (101, WELCOME_CTA_TEXT, None),
    ]


async def test_timezone_text_fallback_known_and_unknown_city() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active")
    pool.add_user(102, status="active")
    cache.values["onboarding:tz_pending:101"] = "1"
    cache.values["onboarding:tz_pending:102"] = "1"

    known = await processor.process(_envelope(tg_user_id=101, text=" Самара "))
    unknown = await processor.process(_envelope(tg_user_id=102, text="Городок"))

    assert known is None
    assert pool.users[101]["timezone"] == "Europe/Samara"
    assert unknown is None
    assert recorder.sent == [
        (101, WELCOME_TEXT.format(tz="Europe/Samara"), None),
        (101, WELCOME_CTA_TEXT, None),
        (102, UNKNOWN_CITY_TEXT, TIMEZONE_BUTTONS),
    ]


async def test_active_help_start_and_passthrough_to_inner_echo() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active")

    assert await processor.process(_envelope(tg_user_id=101, text="/help")) == HELP_TEXT
    assert await processor.process(_envelope(tg_user_id=101, text="/start")) == ALREADY_ACTIVE_TEXT
    assert await processor.process(_envelope(tg_user_id=101, text="echo")) == "echo"
    assert HELP_TEXT == (
        ASSISTANT_CAPABILITIES_TEXT
        + "\n\nЕсли что-то пойдёт не так — напишите /feedback <текст>: прочитаю и исправлюсь."
    )
    assert WELCOME_TEXT == (
        "Часовой пояс сохранён: {tz}. Всё готово 👌\n\n" + ASSISTANT_CAPABILITIES_TEXT
    )


async def test_active_feedback_notifies_admin_and_confirms_user() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active")

    reply = await processor.process(_envelope(tg_user_id=101, text="/feedback\nОчень полезно"))

    assert reply == "Спасибо! Передал команде — это помогает делать ассистента лучше."
    assert recorder.admin == ["Отзыв от 101: Очень полезно"]


async def test_active_feedback_without_text_returns_hint_without_notification() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active")

    reply = await processor.process(_envelope(tg_user_id=101, text="/feedback   \n\t"))

    assert reply == "Напишите отзыв одним сообщением: /feedback <текст>"
    assert recorder.admin == []


async def test_active_feedback_truncates_admin_notification_text() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active")
    feedback = "a" * 1001

    reply = await processor.process(_envelope(tg_user_id=101, text=f"/feedback {feedback}"))

    assert reply == "Спасибо! Передал команде — это помогает делать ассистента лучше."
    assert recorder.admin == [f"Отзыв от 101: {'a' * 1000}"]


async def test_active_feedback_confirms_user_when_admin_notification_fails() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    recorder.notify_admin_error = RuntimeError("admin unavailable")
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active")

    reply = await processor.process(_envelope(tg_user_id=101, text="/feedback Тест"))

    assert reply == "Спасибо! Передал команде — это помогает делать ассистента лучше."
    assert recorder.admin == ["Отзыв от 101: Тест"]


async def test_pending_feedback_autoactivates_before_command_handling() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="pending")

    reply = await processor.process(_envelope(tg_user_id=101, text="/feedback Тест"))

    assert reply is None
    assert pool.users[101]["status"] == "active"
    assert recorder.sent == [(101, START_TIMEZONE_PROMPT_TEXT, TIMEZONE_BUTTONS)]
    assert recorder.admin == []


async def test_active_user_context_uses_the_already_resolved_user_row() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    inner = ContextRecorder()
    processor = OnboardingProcessor(
        service_pool=pool,  # type: ignore[arg-type]
        cache_redis=cache,
        inner=inner,
        send=recorder.send,
        answer_callback=recorder.answer_callback,
        notify_admin=recorder.notify_admin,
        admin_ids=(900,),
    )
    pool.add_user(
        101,
        tg_chat_id=501,
        status="active",
        timezone="Asia/Almaty",
        assistant_profile={"name": "Джарвис", "address": "ty"},
    )
    envelope = _envelope(tg_user_id=101, chat_id=999, text="hello")

    assert await processor.process(envelope) == "received"
    assert inner.context == TaskContext(
        user_id=pool.users[101]["id"],
        tg_user_id=101,
        chat_id=501,
        update_id=envelope.update_id,
        timezone="Asia/Almaty",
        plan="trial",
        trace_id=envelope.trace_id,
        assistant_profile={"name": "Джарвис", "address": "ty"},
    )


def test_onboarding_persona_copy_is_exact_and_ordered() -> None:
    persona_bullet = (
        "🎭 **Персона** — «называй себя Джарвис», «общайся на ты» — имя и стиль сохранятся навсегда"
    )
    persona_cta = (
        "А ещё мне можно дать имя и стиль: «называй себя Джарвис и общайся со мной на ты»."
    )

    assert persona_bullet in ASSISTANT_CAPABILITIES_TEXT
    assert ASSISTANT_CAPABILITIES_TEXT.index("🧠 **Память**") < (
        ASSISTANT_CAPABILITIES_TEXT.index(persona_bullet)
    )
    assert ASSISTANT_CAPABILITIES_TEXT.index(persona_bullet) < (
        ASSISTANT_CAPABILITIES_TEXT.index("Календарь напоминаний")
    )
    assert persona_cta in WELCOME_CTA_TEXT
    assert WELCOME_CTA_TEXT.index(persona_cta) < WELCOME_CTA_TEXT.index("/feedback")
