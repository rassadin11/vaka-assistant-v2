"""Onboarding processor for Telegram users."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from zoneinfo import available_timezones

import asyncpg
import uuid_utils

from core.context import TaskContext
from core.db import service_transaction
from core.envelope import UpdateEnvelope
from worker.processor import ContextualProcessor
from worker.reply import WorkerReply

REJECTED_TEXT = "Пока не получилось открыть доступ — места в бете ограничены. Спасибо за интерес!"
START_TIMEZONE_PROMPT_TEXT = (
    "Привет! Я — персональный ассистент: расходы, напоминания, поиск в интернете, "
    "PDF-документы — всё обычными словами в чате, текстом или голосом.\n\n"
    "Чтобы напоминания приходили вовремя, выберите ваш часовой пояс — по разнице с Москвой:"
)
ASSISTANT_CAPABILITIES_TEXT = (
    "Я — персональный ассистент. Мне не нужны команды: пишите обычными словами — или просто "
    "**надиктуйте голосовое**, я понимаю речь и сделаю всё то же самое. Если непонятно, как "
    "работает бот, или возникла проблема — попробуйте спросить у меня, скорее всего получится "
    "исправить внутри чата.\n\n"
    "Какие возможности у меня есть?\n\n"
    "🎭 Настройте меня как «**Личность**» — «называй себя Ася, ты девушка», «общайся на ты» "
    "или «называй себя Михаил, общаемся официально» — имя и стиль сохранятся навсегда\n"
    "🧠 У меня есть «**Память**» — если сказать «запомни: у меня аллергия на арахис», то я "
    "учту это во всех будущих ответах\n"
    "💰 Могу вести ваши «**Расходы и бюджеты**» — «потратил 300 на кофе», «сколько ушло на "
    "еду в этом месяце?» — все расходы записываются, потом их можно проанализировать в "
    "приложении бота или спросить у меня\n"
    "⏰ Можно поставить «**Напоминания**», попросить меня найти информацию в интернете или "
    "прочитать PDF документ.\n\n"
    "Если хотите узнать о какой-то функции подробнее — спросите меня!\n\n"
    "Календарь напоминаний и дашборд расходов находятся в меню слева от поля ввода."
)
WELCOME_TEXT = "Часовой пояс сохранён: {tz}. Всё готово 👌\n\n" + ASSISTANT_CAPABILITIES_TEXT
WELCOME_CTA_TEXT = (
    "Давайте попробуем прямо сейчас. Напишите — или наговорите голосом — что-нибудь одно:\n\n"
    "«Потратил 450 на такси»\n"
    "«Напомни завтра в 9 позвонить маме»\n"
    "«Запомни: мою собаку зовут Муся»\n\n"
    "Не команда, а просто фраза — как написали бы другу. Любую из них можно просто "
    "наговорить голосовым.\n"
    "Если что-то пойдёт не так — "
    "/feedback <текст>: я в бете и учусь."
)
HELP_TEXT = (
    ASSISTANT_CAPABILITIES_TEXT
    + "\n\nЕсли что-то пойдёт не так — напишите /feedback <текст>: прочитаю и исправлюсь."
)
HINT_FINANCE_TEXT = (
    "Кстати: все расходы наглядно — на дашборде «Финансы» (кнопка меню). А ещё можно задать "
    "бюджет: «поставь бюджет 10 000 на еду в месяц» — предупрежу, когда он будет подходить к "
    "концу."
)
HINT_REMINDER_TEXT = (
    "Кстати: все напоминания видны в календаре (кнопка меню). Могу и повторяющиеся: "
    "«напоминай каждый понедельник в 9 про планёрку»."
)
HINT_VOICE_TEXT = (
    "Кстати: голосовые я понимаю до 5 минут — удобно надиктовывать расходы и напоминания на ходу."
)
ALREADY_ACTIVE_TEXT = "Вы уже в деле! Команда /help напомнит, что я умею."
FEEDBACK_CONFIRMATION_TEXT = "Спасибо! Передал команде — это помогает делать ассистента лучше."
FEEDBACK_HINT_TEXT = "Напишите отзыв одним сообщением: /feedback <текст>"
UNKNOWN_CITY_TEXT = (
    "Не узнал город — выберите кнопкой или пришлите крупный город рядом (например, „Самара“)."
)
OTHER_CITY_TEXT = "Пришлите название города текстом"

TIMEZONE_PENDING_TTL_SECONDS = 604_800
REJECTED_NOTIFIED_TTL_SECONDS = 86_400
PROVISIONAL_TIMEZONE = "Europe/Moscow"

# All eleven Russian time zones, labelled by their offset from Moscow with an anchor city.
TIMEZONE_BUTTONS: list[list[tuple[str, str]]] = [
    [
        ("МСК-1 · Калининград", "tz:Europe/Kaliningrad"),
        ("МСК · Москва, Санкт-Петербург", "tz:Europe/Moscow"),
    ],
    [
        ("МСК+1 · Самара", "tz:Europe/Samara"),
        ("МСК+2 · Екатеринбург", "tz:Asia/Yekaterinburg"),
    ],
    [
        ("МСК+3 · Омск", "tz:Asia/Omsk"),
        ("МСК+4 · Красноярск", "tz:Asia/Krasnoyarsk"),
    ],
    [
        ("МСК+5 · Иркутск", "tz:Asia/Irkutsk"),
        ("МСК+6 · Якутск", "tz:Asia/Yakutsk"),
    ],
    [
        ("МСК+7 · Владивосток", "tz:Asia/Vladivostok"),
        ("МСК+8 · Магадан", "tz:Asia/Magadan"),
    ],
    [("МСК+9 · Камчатка", "tz:Asia/Kamchatka")],
    [("Другой город — напишу текстом", "tz:other")],
]

CITY_TZ: dict[str, str] = {
    # UTC+2 (MSK-1)
    "калининград": "Europe/Kaliningrad",
    # UTC+3 (MSK)
    "москва": "Europe/Moscow",
    "санкт-петербург": "Europe/Moscow",
    "петербург": "Europe/Moscow",
    "нижний новгород": "Europe/Moscow",
    "казань": "Europe/Moscow",
    "ростов-на-дону": "Europe/Moscow",
    "краснодар": "Europe/Moscow",
    "воронеж": "Europe/Moscow",
    "волгоград": "Europe/Volgograd",
    "сочи": "Europe/Moscow",
    "мурманск": "Europe/Moscow",
    "архангельск": "Europe/Moscow",
    "ярославль": "Europe/Moscow",
    "тула": "Europe/Moscow",
    "рязань": "Europe/Moscow",
    "симферополь": "Europe/Simferopol",
    "севастополь": "Europe/Simferopol",
    "минск": "Europe/Minsk",
    # UTC+4 (MSK+1)
    "самара": "Europe/Samara",
    "саратов": "Europe/Saratov",
    "тольятти": "Europe/Samara",
    "ижевск": "Europe/Samara",
    "ульяновск": "Europe/Ulyanovsk",
    "астрахань": "Europe/Astrakhan",
    # UTC+5 (MSK+2)
    "екатеринбург": "Asia/Yekaterinburg",
    "челябинск": "Asia/Yekaterinburg",
    "уфа": "Asia/Yekaterinburg",
    "пермь": "Asia/Yekaterinburg",
    "тюмень": "Asia/Yekaterinburg",
    "сургут": "Asia/Yekaterinburg",
    "оренбург": "Asia/Yekaterinburg",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "ташкент": "Asia/Tashkent",
    # UTC+6 (MSK+3)
    "омск": "Asia/Omsk",
    "бишкек": "Asia/Bishkek",
    # UTC+7 (MSK+4)
    "новосибирск": "Asia/Novosibirsk",
    "красноярск": "Asia/Krasnoyarsk",
    "барнаул": "Asia/Barnaul",
    "томск": "Asia/Tomsk",
    "кемерово": "Asia/Novokuznetsk",
    "новокузнецк": "Asia/Novokuznetsk",
    # UTC+8 (MSK+5)
    "иркутск": "Asia/Irkutsk",
    "улан-удэ": "Asia/Irkutsk",
    "братск": "Asia/Irkutsk",
    # UTC+9 (MSK+6)
    "якутск": "Asia/Yakutsk",
    "чита": "Asia/Chita",
    "благовещенск": "Asia/Yakutsk",
    # UTC+10 (MSK+7)
    "владивосток": "Asia/Vladivostok",
    "хабаровск": "Asia/Vladivostok",
    # UTC+11 (MSK+8)
    "магадан": "Asia/Magadan",
    "южно-сахалинск": "Asia/Sakhalin",
    # UTC+12 (MSK+9)
    "камчатка": "Asia/Kamchatka",
    "петропавловск-камчатский": "Asia/Kamchatka",
    "анадырь": "Asia/Anadyr",
    # Other CIS capitals
    "ереван": "Asia/Yerevan",
    "тбилиси": "Asia/Tbilisi",
    "баку": "Asia/Baku",
    "киев": "Europe/Kyiv",
    "кишинёв": "Europe/Chisinau",
}

VALID_TIMEZONES = frozenset(available_timezones())

LOGGER = logging.getLogger(__name__)


class OnboardingHintAxis(StrEnum):
    """One-time onboarding hint categories."""

    FINANCE = "finance"
    REMINDER = "reminder"
    VOICE = "voice"


class HintRedis(Protocol):
    """Redis command required for permanent onboarding hint claims."""

    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> Awaitable[object]: ...


class CacheRedis(HintRedis, Protocol):
    """Subset of Redis cache commands used by onboarding."""

    def exists(self, name: str) -> Awaitable[int]: ...

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
    assistant_profile: dict[str, str] | None = None


def _decode_assistant_profile(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    profile = {
        key: item
        for key, item in decoded.items()
        if key in {"name", "address", "style"} and isinstance(item, str)
    }
    return profile or None


class OnboardingProcessor:
    """Wrap an inner processor with access control and onboarding."""

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
            await self._create_active_user(envelope)
            await self._start_timezone_selection(envelope.user_id, envelope.chat_id)
            await self._notify_admin(f"Новый пользователь: id {envelope.user_id}.")
            return None

        if user.status == "pending":
            await self._activate_pending_user(user.tg_user_id)
            await self._start_timezone_selection(user.tg_user_id, user.tg_chat_id)
            return None
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
        if command == "/reject":
            if len(args) != 1:
                return _admin_usage()
            tg_user_id = _parse_tg_user_id(args[0])
            if tg_user_id is None:
                return _admin_usage()
            return await self._reject_user(tg_user_id)
        return None

    async def _activate_pending_user(self, tg_user_id: int) -> None:
        async with service_transaction(self._service_pool) as connection:
            await connection.execute(
                """
                UPDATE users
                SET status = 'active', updated_at = now()
                WHERE tg_user_id = $1 AND status = 'pending'
                """,
                tg_user_id,
            )

    async def _start_timezone_selection(self, tg_user_id: int, chat_id: int) -> None:
        await self._cache_redis.set(
            _tz_pending_key(tg_user_id),
            "1",
            ex=TIMEZONE_PENDING_TTL_SECONDS,
        )
        await self._send(chat_id, START_TIMEZONE_PROMPT_TEXT, TIMEZONE_BUTTONS)

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
                SELECT id, tg_user_id, tg_chat_id, status, timezone, plan,
                       assistant_profile::text AS assistant_profile
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
            assistant_profile=_decode_assistant_profile(row["assistant_profile"]),
        )

    async def _create_active_user(self, envelope: UpdateEnvelope) -> None:
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
                VALUES ($1, $2, $3, 'active', $4, now(), now())
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
            assistant_profile=user.assistant_profile,
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
        await self._save_timezone(envelope.user_id, timezone)
        return None

    async def _process_timezone_text(self, envelope: UpdateEnvelope, text: str) -> str | None:
        timezone = CITY_TZ.get(text.strip().lower())
        if timezone is None:
            await self._send(envelope.chat_id, UNKNOWN_CITY_TEXT, TIMEZONE_BUTTONS)
            return None
        await self._save_timezone(envelope.user_id, timezone)
        return None

    async def _save_timezone(self, tg_user_id: int, timezone: str) -> None:
        async with service_transaction(self._service_pool) as connection:
            row = await connection.fetchrow(
                """
                UPDATE users
                SET timezone = $2, updated_at = now()
                WHERE tg_user_id = $1
                RETURNING tg_chat_id
                """,
                tg_user_id,
                timezone,
            )
        if row is None:
            self._logger.warning("timezone save target disappeared: tg_user_id=%s", tg_user_id)
            return
        await self._cache_redis.delete(_tz_pending_key(tg_user_id))
        await self._send(row["tg_chat_id"], WELCOME_TEXT.format(tz=timezone))
        await self._send(row["tg_chat_id"], WELCOME_CTA_TEXT)


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
    return "Использование: /reject <tg_user_id>."


def _tz_pending_key(tg_user_id: int) -> str:
    return f"onboarding:tz_pending:{tg_user_id}"


def _rejected_notified_key(tg_user_id: int) -> str:
    return f"onboarding:rejected_notified:{tg_user_id}"


async def claim_onboarding_hint(
    redis: HintRedis,
    axis: OnboardingHintAxis,
    tg_user_id: int,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Claim a permanent one-time hint slot, failing open on Redis errors."""

    active_logger = logger if logger is not None else LOGGER
    try:
        return bool(
            await redis.set(
                f"onboarding:hint:{axis.value}:{tg_user_id}",
                "1",
                nx=True,
            )
        )
    except Exception:
        active_logger.warning(
            "onboarding hint claim failed: axis=%s tg_user_id=%s",
            axis.value,
            tg_user_id,
            exc_info=True,
        )
        return False
