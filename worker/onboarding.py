"""Closed-beta onboarding processor for Telegram users."""

# ruff: noqa: RUF001

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol
from zoneinfo import available_timezones

import asyncpg
import uuid_utils

from core.context import TaskContext
from core.db import service_transaction
from core.envelope import UpdateEnvelope
from worker.processor import ContextualProcessor
from worker.reply import WorkerReply

APPLICATION_RECEIVED_TEXT = (
    "Привет! Это закрытая бета персонального ассистента. Заявка на доступ отправлена — "
    "напишу, как только её одобрят."
)
PENDING_REPEAT_TEXT = "Заявка ещё на рассмотрении — напишу, как только будет решение."
REJECTED_TEXT = "Пока не получилось открыть доступ — места в бете ограничены. Спасибо за интерес!"
TIMEZONE_PROMPT_TEXT = (
    "Доступ открыт! Чтобы напоминания приходили вовремя, выберите ваш город — "
    "так я узнаю часовой пояс:"
)
ASSISTANT_CAPABILITIES_TEXT = (
    "Я персональный ассистент. Что умею:\n"
    "• вести учёт расходов и бюджеты — «потратил 750 на обед», «сколько я трачу на еду?»\n"
    "• напоминать о делах — «напомни завтра в 10 позвонить маме»\n"
    "• искать в интернете — «что нового у Яндекса? поищи»\n"
    "• разбирать PDF-документы — пришлите файл и спрашивайте по содержимому\n"
    "• запоминать важное — «запомни: у меня аллергия на арахис»\n"
    "• показать наглядно — кнопка меню открывает календарь напоминаний и дашборд расходов\n\n"
    "Пишите как удобно, своими словами — я пойму. Я в бете и учусь: если что-то пойдёт не так, "
    "напишите /feedback <текст> — прочитаю и исправлюсь."
)
WELCOME_TEXT = "Часовой пояс сохранён: {tz}.\n\n" + ASSISTANT_CAPABILITIES_TEXT
HELP_TEXT = ASSISTANT_CAPABILITIES_TEXT
ALREADY_ACTIVE_TEXT = "Вы уже в деле! Команда /help напомнит, что я умею."
FEEDBACK_CONFIRMATION_TEXT = "Спасибо! Передал команде — это помогает делать ассистента лучше."
FEEDBACK_HINT_TEXT = "Напишите отзыв одним сообщением: /feedback <текст>"
ADMIN_NEW_APPLICATION_TEXT = (
    "Новая заявка: id {tg_user_id}. /approve {tg_user_id} или /reject {tg_user_id}"
)
UNKNOWN_CITY_TEXT = (
    "Не узнал город — выберите кнопкой или пришлите крупный город рядом (например, „Самара“)."
)
OTHER_CITY_TEXT = "Пришлите название города текстом"

TIMEZONE_PENDING_TTL_SECONDS = 604_800
REJECTED_NOTIFIED_TTL_SECONDS = 86_400
PROVISIONAL_TIMEZONE = "Europe/Moscow"

TIMEZONE_BUTTONS: list[list[tuple[str, str]]] = [
    [("Москва", "tz:Europe/Moscow"), ("Санкт-Петербург", "tz:Europe/Moscow")],
    [("Калининград", "tz:Europe/Kaliningrad"), ("Екатеринбург", "tz:Asia/Yekaterinburg")],
    [("Новосибирск", "tz:Asia/Novosibirsk"), ("Владивосток", "tz:Asia/Vladivostok")],
    [("Минск", "tz:Europe/Minsk"), ("Алматы", "tz:Asia/Almaty")],
    [("Другой город — напишу текстом", "tz:other")],
]

CITY_TZ: dict[str, str] = {
    "москва": "Europe/Moscow",
    "санкт-петербург": "Europe/Moscow",
    "калининград": "Europe/Kaliningrad",
    "екатеринбург": "Asia/Yekaterinburg",
    "новосибирск": "Asia/Novosibirsk",
    "владивосток": "Asia/Vladivostok",
    "минск": "Europe/Minsk",
    "алматы": "Asia/Almaty",
    "самара": "Europe/Samara",
    "саратов": "Europe/Saratov",
    "омск": "Asia/Omsk",
    "красноярск": "Asia/Krasnoyarsk",
    "иркутск": "Asia/Irkutsk",
    "якутск": "Asia/Yakutsk",
    "ташкент": "Asia/Tashkent",
    "ереван": "Asia/Yerevan",
    "тбилиси": "Asia/Tbilisi",
    "баку": "Asia/Baku",
    "астана": "Asia/Almaty",
    "бишкек": "Asia/Bishkek",
    "киев": "Europe/Kyiv",
    "кишинёв": "Europe/Chisinau",
}

VALID_TIMEZONES = frozenset(available_timezones())

LOGGER = logging.getLogger(__name__)


class CacheRedis(Protocol):
    """Subset of Redis cache commands used by onboarding."""

    def exists(self, name: str) -> Awaitable[int]: ...

    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> Awaitable[object]: ...

    def delete(self, *names: str) -> Awaitable[int]: ...


class SendCallback(Protocol):
    """Send a Telegram message with optional button rows."""

    def __call__(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[tuple[str, str]]] | None = None,
    ) -> Awaitable[None]: ...


class AnswerCallback(Protocol):
    """Answer a Telegram callback query."""

    def __call__(self, callback_query_id: str) -> Awaitable[None]: ...


class NotifyAdmin(Protocol):
    """Notify configured admins."""

    def __call__(self, text: str) -> Awaitable[None]: ...


class ConfirmationHandler(Protocol):
    """Handle confirmation callbacks after the active user has been resolved."""

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None: ...


@dataclass(frozen=True, slots=True)
class UserRow:
    """Resolved Telegram user state."""

    id: uuid.UUID
    tg_user_id: int
    tg_chat_id: int
    status: str
    timezone: str
    plan: str


class OnboardingProcessor:
    """Wrap an inner processor with closed-beta access control and onboarding."""

    def __init__(
        self,
        *,
        service_pool: asyncpg.Pool,
        cache_redis: CacheRedis,
        inner: ContextualProcessor,
        send: SendCallback,
        answer_callback: AnswerCallback,
        notify_admin: NotifyAdmin,
        admin_ids: tuple[int, ...],
        confirmation_handler: ConfirmationHandler | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service_pool = service_pool
        self._cache_redis = cache_redis
        self._inner = inner
        self._send = send
        self._answer_callback = answer_callback
        self._notify_admin = notify_admin
        self._admin_ids = frozenset(admin_ids)
        self._confirmation_handler = confirmation_handler
        self._logger = logger if logger is not None else LOGGER

    async def process(self, envelope: UpdateEnvelope) -> str | WorkerReply | None:
        """Process onboarding state, delegating active user traffic to the inner processor."""

        if envelope.kind == "callback":
            await self._answer_callback_first(envelope)

        text = _payload_text(envelope)
        if text is not None and envelope.user_id in self._admin_ids:
            admin_reply = await self._try_admin_command(envelope, text)
            if admin_reply is not None:
                return admin_reply

        user = await self._resolve_user(envelope.user_id)
        if user is None:
            await self._create_pending_user(envelope)
            await self._notify_admin(ADMIN_NEW_APPLICATION_TEXT.format(tg_user_id=envelope.user_id))
            return APPLICATION_RECEIVED_TEXT

        if user.status == "pending":
            return PENDING_REPEAT_TEXT
        if user.status == "rejected":
            return await self._rejected_reply_once_per_day(envelope.user_id)
        if user.status == "banned":
            return None
        if user.status != "active":
            self._logger.warning("unknown onboarding status: %s", user.status)
            return None

        return await self._process_active_user(envelope, text, user)

    async def _answer_callback_first(self, envelope: UpdateEnvelope) -> None:
        callback_query_id = envelope.payload.get("callback_query_id")
        if not isinstance(callback_query_id, str) or callback_query_id == "":
            return
        try:
            await self._answer_callback(callback_query_id)
        except Exception:
            self._logger.warning("callback answer failed", exc_info=True)

    async def _try_admin_command(self, envelope: UpdateEnvelope, text: str) -> str | None:
        command, *args = text.strip().split()
        if command == "/pending":
            if args:
                return _admin_usage()
            return await self._pending_users_text()
        if command == "/approve":
            if len(args) != 1:
                return _admin_usage()
            tg_user_id = _parse_tg_user_id(args[0])
            if tg_user_id is None:
                return _admin_usage()
            return await self._approve_user(tg_user_id)
        if command == "/reject":
            if len(args) != 1:
                return _admin_usage()
            tg_user_id = _parse_tg_user_id(args[0])
            if tg_user_id is None:
                return _admin_usage()
            return await self._reject_user(tg_user_id)
        return None

    async def _pending_users_text(self) -> str:
        async with service_transaction(self._service_pool) as connection:
            rows = await connection.fetch(
                """
                SELECT tg_user_id, first_name, username, created_at
                FROM users
                WHERE status = 'pending'
                ORDER BY created_at NULLS LAST, tg_user_id
                """
            )
        if not rows:
            return "Нет заявок на рассмотрении."

        lines = ["Заявки на рассмотрении:"]
        for row in rows:
            first_name = _optional_str(row, "first_name") or "-"
            username = _optional_str(row, "username")
            username_text = f"@{username}" if username else "-"
            created_at = row["created_at"]
            lines.append(f"{row['tg_user_id']} | {first_name} | {username_text} | {created_at}")
        return "\n".join(lines)

    async def _approve_user(self, tg_user_id: int) -> str:
        async with service_transaction(self._service_pool) as connection:
            row = await connection.fetchrow(
                """
                UPDATE users
                SET status = 'active', updated_at = now()
                WHERE tg_user_id = $1
                RETURNING tg_chat_id
                """,
                tg_user_id,
            )
        if row is None:
            return f"Пользователь {tg_user_id} не найден."

        await self._cache_redis.set(
            _tz_pending_key(tg_user_id),
            "1",
            ex=TIMEZONE_PENDING_TTL_SECONDS,
        )
        await self._send(row["tg_chat_id"], TIMEZONE_PROMPT_TEXT, TIMEZONE_BUTTONS)
        return f"Пользователь {tg_user_id} одобрен."

    async def _reject_user(self, tg_user_id: int) -> str:
        async with service_transaction(self._service_pool) as connection:
            row = await connection.fetchrow(
                """
                UPDATE users
                SET status = 'rejected', updated_at = now()
                WHERE tg_user_id = $1
                RETURNING tg_chat_id
                """,
                tg_user_id,
            )
        if row is None:
            return f"Пользователь {tg_user_id} не найден."

        await self._send(row["tg_chat_id"], REJECTED_TEXT)
        return f"Пользователь {tg_user_id} отклонён."

    async def _resolve_user(self, tg_user_id: int) -> UserRow | None:
        async with service_transaction(self._service_pool) as connection:
            row = await connection.fetchrow(
                """
                SELECT id, tg_user_id, tg_chat_id, status, timezone, plan
                FROM users
                WHERE tg_user_id = $1
                """,
                tg_user_id,
            )
        if row is None:
            return None
        return UserRow(
            id=row["id"],
            tg_user_id=row["tg_user_id"],
            tg_chat_id=row["tg_chat_id"],
            status=row["status"],
            timezone=row["timezone"],
            plan=row["plan"],
        )

    async def _create_pending_user(self, envelope: UpdateEnvelope) -> None:
        user_id = uuid.UUID(str(uuid_utils.uuid7()))
        async with service_transaction(self._service_pool) as connection:
            await connection.execute(
                """
                INSERT INTO users (
                    id,
                    tg_user_id,
                    tg_chat_id,
                    status,
                    timezone,
                    created_at,
                    updated_at
                )
                VALUES ($1, $2, $3, 'pending', $4, now(), now())
                """,
                user_id,
                envelope.user_id,
                envelope.chat_id,
                PROVISIONAL_TIMEZONE,
            )

    async def _rejected_reply_once_per_day(self, tg_user_id: int) -> str | None:
        should_reply = await self._cache_redis.set(
            _rejected_notified_key(tg_user_id),
            "1",
            ex=REJECTED_NOTIFIED_TTL_SECONDS,
            nx=True,
        )
        if bool(should_reply):
            return REJECTED_TEXT
        return None

    async def _process_active_user(
        self,
        envelope: UpdateEnvelope,
        text: str | None,
        user: UserRow,
    ) -> str | WorkerReply | None:
        context = TaskContext(
            user_id=user.id,
            tg_user_id=user.tg_user_id,
            chat_id=user.tg_chat_id,
            update_id=envelope.update_id,
            timezone=user.timezone,
            plan=user.plan,
            trace_id=envelope.trace_id,
        )
        data = envelope.payload.get("data")
        if (
            envelope.kind == "callback"
            and isinstance(data, str)
            and data.startswith(("confirm:", "cancel:"))
            and self._confirmation_handler is not None
        ):
            return await self._confirmation_handler.process(envelope, context)
        if envelope.kind == "callback" and isinstance(data, str) and data.startswith("tz:"):
            return await self._process_timezone_callback(envelope, data)

        if text is not None and await self._cache_redis.exists(_tz_pending_key(envelope.user_id)):
            return await self._process_timezone_text(envelope, text)

        if text == "/help":
            return HELP_TEXT
        if text == "/start":
            return ALREADY_ACTIVE_TEXT
        feedback = _feedback_text(text) if text is not None else None
        if feedback is not None:
            if feedback == "":
                return FEEDBACK_HINT_TEXT
            try:
                await self._notify_admin(f"Отзыв от {envelope.user_id}: {feedback[:1000]}")
            except Exception:
                self._logger.warning("feedback notification failed", exc_info=True)
            return FEEDBACK_CONFIRMATION_TEXT
        return await self._inner.process(envelope, context)

    async def _process_timezone_callback(self, envelope: UpdateEnvelope, data: str) -> str | None:
        timezone = data.removeprefix("tz:")
        if timezone == "other":
            return OTHER_CITY_TEXT
        if timezone not in VALID_TIMEZONES:
            return None
        return await self._save_timezone(envelope.user_id, timezone)

    async def _process_timezone_text(self, envelope: UpdateEnvelope, text: str) -> str | None:
        timezone = CITY_TZ.get(text.strip().lower())
        if timezone is None:
            await self._send(envelope.chat_id, UNKNOWN_CITY_TEXT, TIMEZONE_BUTTONS)
            return None
        return await self._save_timezone(envelope.user_id, timezone)

    async def _save_timezone(self, tg_user_id: int, timezone: str) -> str:
        async with service_transaction(self._service_pool) as connection:
            await connection.execute(
                """
                UPDATE users
                SET timezone = $2, updated_at = now()
                WHERE tg_user_id = $1
                """,
                tg_user_id,
                timezone,
            )
        await self._cache_redis.delete(_tz_pending_key(tg_user_id))
        return WELCOME_TEXT.format(tz=timezone)


def _payload_text(envelope: UpdateEnvelope) -> str | None:
    text = envelope.payload.get("text")
    return text if isinstance(text, str) else None


def _feedback_text(text: str) -> str | None:
    if text == "/feedback":
        return ""
    if not text.startswith("/feedback"):
        return None
    suffix = text.removeprefix("/feedback")
    if suffix == "" or not suffix[0].isspace():
        return None
    return suffix.strip()


def _parse_tg_user_id(raw_value: str) -> int | None:
    try:
        return int(raw_value)
    except ValueError:
        return None


def _admin_usage() -> str:
    return "Использование: /pending, /approve <tg_user_id>, /reject <tg_user_id>."


def _tz_pending_key(tg_user_id: int) -> str:
    return f"onboarding:tz_pending:{tg_user_id}"


def _rejected_notified_key(tg_user_id: int) -> str:
    return f"onboarding:rejected_notified:{tg_user_id}"


def _optional_str(row: MappingRow, key: str) -> str | None:
    value = row[key]
    return value if isinstance(value, str) else None


class MappingRow(Protocol):
    """Minimal row lookup protocol shared by asyncpg records and test fakes."""

    def __getitem__(self, key: str) -> Any: ...
