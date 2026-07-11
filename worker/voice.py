"""Voice-envelope processing that converts successful STT into normal text work."""

# ruff: noqa: RUF001

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from core.limits import message_limit_reached
from core.spend import BudgetState, add_spend, budget_state, daily_budget_rub, get_spent_rub
from core.stt import STTProvider, STTUnavailableError
from core.usage_store import save_stt_usage
from worker.documents import MAX_DOCUMENT_BYTES, DownloadFile, QueueRedis
from worker.processor import ContextualProcessor

MAX_VOICE_DURATION_SECONDS = 300
MAX_DAILY_VOICE_MINUTES = 10
VOICE_COUNTER_TTL_SECONDS = 2 * 86_400

TOO_LONG_TEXT = "Голосовые длиннее 5 минут не поддерживаются."
DAILY_LIMIT_TEXT = "Превышен дневной лимит голосовых сообщений."
STT_UNAVAILABLE_TEXT = "Не получилось распознать голосовое, попробуйте позже или напишите текстом"
EMPTY_TRANSCRIPT_TEXT = "Не удалось разобрать голосовое, попробуйте ещё раз или напишите текстом"
VOICE_UNAVAILABLE_TEXT = "Голосовые сообщения временно недоступны, напишите текстом"
SOFT_REFUSE_TEXT = (
    "На сегодня дневной лимит ассистента исчерпан. Продолжим после полуночи — "
    "или напишите /feedback, если лимит мешает"
)
MESSAGE_LIMIT_TEXT = (
    "На сегодня лимит сообщений исчерпан. Продолжим после полуночи — "
    "или напишите /feedback, если лимита не хватает"
)

LOGGER = logging.getLogger(__name__)


class VoiceProcessor:
    """Process voice jobs without permitting expected failures to reach the DLQ."""

    def __init__(
        self,
        app_pool: asyncpg.Pool,
        queue_redis: QueueRedis,
        download_file: DownloadFile | None,
        stt_provider: STTProvider | None,
        inner: ContextualProcessor,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._app_pool = app_pool
        self._queue_redis = queue_redis
        self._download_file = download_file
        self._stt_provider = stt_provider
        self._inner = inner
        self._logger = logger if logger is not None else LOGGER

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        """Apply limits, transcribe, and pass the rewritten text envelope to the agent."""

        try:
            if await message_limit_reached(
                self._queue_redis, context.user_id, context.plan, context.timezone
            ):
                return MESSAGE_LIMIT_TEXT
            if await self._budget_state(context) is BudgetState.SOFT_REFUSE:
                return SOFT_REFUSE_TEXT
            duration = _duration(envelope.payload.get("duration"))
            if duration is None:
                return STT_UNAVAILABLE_TEXT
            if duration > MAX_VOICE_DURATION_SECONDS:
                return TOO_LONG_TEXT
            if self._stt_provider is None or self._download_file is None:
                return VOICE_UNAVAILABLE_TEXT

            minutes = math.ceil(duration / 60)
            if not await self._within_daily_limit(context, minutes):
                return DAILY_LIMIT_TEXT

            file_id = envelope.payload.get("tg_file_id")
            if not isinstance(file_id, str) or not file_id:
                return STT_UNAVAILABLE_TEXT
            audio = await self._download_file(file_id, MAX_DOCUMENT_BYTES)
            try:
                result = await self._stt_provider.transcribe(audio, "voice.ogg")
            except STTUnavailableError:
                return STT_UNAVAILABLE_TEXT
            if not result.text.strip():
                return EMPTY_TRANSCRIPT_TEXT

            await self._increment_minutes(context, minutes)
            if await self._save_usage(context, result.cost_usd):
                await add_spend(
                    self._queue_redis,
                    context.user_id,
                    context.timezone,
                    result.cost_usd,
                )
            rewritten = envelope.model_copy(
                update={
                    "kind": "text",
                    "payload": {
                        "text": result.text,
                        "modality": "voice",
                        "duration": duration,
                    },
                }
            )
            return await self._inner.process(rewritten, context)
        except Exception:
            self._logger.exception(
                "voice processing failed", extra={"update_id": envelope.update_id}
            )
            return STT_UNAVAILABLE_TEXT

    async def _within_daily_limit(self, context: TaskContext, minutes: int) -> bool:
        raw_count = await self._queue_redis.get(_daily_minutes_key(context))
        raw_text = raw_count.decode("utf-8") if isinstance(raw_count, bytes) else raw_count
        current = int(raw_text or "0")
        return current + minutes <= MAX_DAILY_VOICE_MINUTES

    async def _increment_minutes(self, context: TaskContext, minutes: int) -> None:
        value = await self._queue_redis.incrby(_daily_minutes_key(context), minutes)
        if value == minutes:
            await self._queue_redis.expire(_daily_minutes_key(context), VOICE_COUNTER_TTL_SECONDS)

    async def _budget_state(self, context: TaskContext) -> BudgetState:
        """Read the daily budget once before any voice download or STT work."""

        spent = await get_spent_rub(self._queue_redis, context.user_id, context.timezone)
        return budget_state(spent, daily_budget_rub(context.plan))

    async def _save_usage(self, context: TaskContext, cost_usd: Decimal) -> bool:
        try:
            await save_stt_usage(self._app_pool, context.user_id, context.trace_id, cost_usd)
            return True
        except Exception:
            self._logger.exception(
                "failed to save STT usage", extra={"trace_id": str(context.trace_id)}
            )
            return False


def _daily_minutes_key(context: TaskContext) -> str:
    """Build the UTC daily Redis key for a user's consumed voice minutes."""

    return f"stt_min:{context.user_id}:{datetime.now(UTC):%Y%m%d}"


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    duration = float(value)
    return duration if math.isfinite(duration) and duration >= 0 else None
