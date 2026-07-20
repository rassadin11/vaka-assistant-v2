"""Unit tests for closed-beta onboarding behavior."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from core.timezones import CITY_TZ, TIMEZONE_BUTTONS, VALID_TIMEZONES
from worker.onboarding import (
    ALREADY_ACTIVE_TEXT,
    ASSISTANT_CAPABILITIES_TEXT,
    CHANGE_TIMEZONE_PROMPT_TEXT,
    HELP_TEXT,
    RECURRING_REMINDERS_SHIFTED_TEXT,
    REJECTED_TEXT,
    START_TIMEZONE_PROMPT_TEXT,
    TIMEZONE_CHANGED_TEXT,
    TZ_PENDING_CHANGE,
    TZ_PENDING_ONBOARDING,
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
        self.recurring_reminders = 0
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
            return {"id": row["id"], "tg_chat_id": row["tg_chat_id"]}
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

    async def fetchval(self, query: str, *args: object) -> object:
        del args
        if "FROM scheduled_tasks" in query:
            return self._pool.recurring_reminders
        raise AssertionError(f"unexpected query: {query}")

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

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

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
    assert cache.values["onboarding:tz_pending:101"] == TZ_PENDING_ONBOARDING
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
    assert cache.values["onboarding:tz_pending:101"] == TZ_PENDING_ONBOARDING
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
    cache.values["onboarding:tz_pending:101"] = TZ_PENDING_ONBOARDING

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
    cache.values["onboarding:tz_pending:101"] = TZ_PENDING_ONBOARDING
    cache.values["onboarding:tz_pending:102"] = TZ_PENDING_ONBOARDING

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
        "🎭 Настройте меня как «**Личность**» — «называй себя Ася, ты девушка», «общайся на ты» "
        "или «называй себя Михаил, общаемся официально» — имя и стиль сохранятся навсегда"
    )
    assert persona_bullet in ASSISTANT_CAPABILITIES_TEXT
    assert ASSISTANT_CAPABILITIES_TEXT.index(persona_bullet) < (
        ASSISTANT_CAPABILITIES_TEXT.index("🧠 У меня есть «**Память**»")
    )
    assert ASSISTANT_CAPABILITIES_TEXT.index(persona_bullet) < (
        ASSISTANT_CAPABILITIES_TEXT.index("Календарь напоминаний")
    )
    assert "имя и стиль" not in WELCOME_CTA_TEXT


def test_timezone_buttons_cover_every_russian_zone_with_moscow_offsets() -> None:
    buttons = [button for row in TIMEZONE_BUTTONS for button in row]
    zones = [callback.removeprefix("tz:") for _label, callback in buttons]
    assert zones[-1] == "other"

    russian_zones = zones[:-1]
    assert russian_zones == [
        "Europe/Kaliningrad",
        "Europe/Moscow",
        "Europe/Samara",
        "Asia/Yekaterinburg",
        "Asia/Omsk",
        "Asia/Krasnoyarsk",
        "Asia/Irkutsk",
        "Asia/Yakutsk",
        "Asia/Vladivostok",
        "Asia/Magadan",
        "Asia/Kamchatka",
    ]
    assert all(zone in VALID_TIMEZONES for zone in russian_zones)

    winter = datetime(2026, 1, 15, 12, tzinfo=UTC)
    moscow_offset = winter.astimezone(ZoneInfo("Europe/Moscow")).utcoffset()
    assert moscow_offset is not None
    for label, callback in buttons[:-1]:
        offset = winter.astimezone(ZoneInfo(callback.removeprefix("tz:"))).utcoffset()
        assert offset is not None
        hours = round((offset - moscow_offset).total_seconds() / 3600)
        expected = "МСК" if hours == 0 else f"МСК{hours:+d}"
        assert label.split(" · ")[0] == expected, label


def test_timezone_text_fallback_maps_known_cities_to_valid_zones() -> None:
    assert set(CITY_TZ.values()) <= VALID_TIMEZONES
    assert all(city == city.strip().lower() for city in CITY_TZ)
    button_zones = {
        callback.removeprefix("tz:")
        for row in TIMEZONE_BUTTONS
        for _label, callback in row
        if callback != "tz:other"
    }
    assert button_zones <= set(CITY_TZ.values())
    # CIS cities lost their buttons, so the text fallback must still resolve them.
    assert CITY_TZ["минск"] == "Europe/Minsk"
    assert CITY_TZ["алматы"] == "Asia/Almaty"


async def test_timezone_command_arms_change_mode_and_shows_current_zone() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active", timezone="Europe/Moscow")

    reply = await processor.process(_envelope(tg_user_id=101, text="/timezone"))

    assert reply is None
    assert cache.values["onboarding:tz_pending:101"] == TZ_PENDING_CHANGE
    assert recorder.sent == [
        (101, CHANGE_TIMEZONE_PROMPT_TEXT.format(tz="Europe/Moscow"), TIMEZONE_BUTTONS),
    ]


async def test_changing_timezone_confirms_briefly_instead_of_repeating_the_welcome() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active", timezone="Europe/Moscow")
    cache.values["onboarding:tz_pending:101"] = TZ_PENDING_CHANGE

    reply = await processor.process(_envelope(tg_user_id=101, text="Новосибирск"))

    assert reply is None
    assert pool.users[101]["timezone"] == "Asia/Novosibirsk"
    assert "onboarding:tz_pending:101" in cache.deleted
    (chat_id, text, buttons) = recorder.sent[0]
    assert len(recorder.sent) == 1
    assert chat_id == 101
    assert buttons is None
    assert text.startswith("Часовой пояс обновлён: Asia/Novosibirsk, сейчас у вас ")
    assert WELCOME_CTA_TEXT not in text


async def test_changing_timezone_mentions_recurring_reminders_only_when_present() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.recurring_reminders = 2
    pool.add_user(101, status="active", timezone="Europe/Moscow")
    pool.add_user(102, status="active", timezone="Europe/Moscow")
    cache.values["onboarding:tz_pending:101"] = TZ_PENDING_CHANGE

    await processor.process(_envelope(tg_user_id=101, text="Омск"))
    shifted = recorder.sent[-1][1]

    pool.recurring_reminders = 0
    cache.values["onboarding:tz_pending:102"] = TZ_PENDING_CHANGE
    await processor.process(_envelope(tg_user_id=102, text="Омск"))
    quiet = recorder.sent[-1][1]

    assert shifted.endswith(RECURRING_REMINDERS_SHIFTED_TEXT.format(count=2))
    assert "Повторяющиеся напоминания" not in quiet


async def test_stale_onboarding_keyboard_does_not_resend_the_welcome() -> None:
    pool = FakePool()
    cache = FakeCache()
    recorder = Recorder()
    processor = _processor(pool, cache, recorder)
    pool.add_user(101, status="active", timezone="Europe/Moscow")

    reply = await processor.process(
        _envelope(
            tg_user_id=101,
            kind="callback",
            payload={
                "data": "tz:Asia/Omsk",
                "message_id": 7,
                "callback_query_id": "cb-stale",
            },
        )
    )

    assert reply is None
    assert pool.users[101]["timezone"] == "Asia/Omsk"
    assert len(recorder.sent) == 1
    assert recorder.sent[0][1].startswith("Часовой пояс обновлён: Asia/Omsk")


def test_timezone_change_texts_match_the_registry_spec() -> None:
    assert TIMEZONE_CHANGED_TEXT == "Часовой пояс обновлён: {tz}, сейчас у вас {local_time}."
    assert CHANGE_TIMEZONE_PROMPT_TEXT == (
        "Ваш часовой пояс сейчас: {tz}. Выберите новый — по разнице с Москвой:"
    )
    assert TZ_PENDING_ONBOARDING != TZ_PENDING_CHANGE
