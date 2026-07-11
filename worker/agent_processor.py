"""Worker adapter that adds trusted context and dialogue persistence to the agent loop."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal
from math import ceil
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from core.agent import AgentLoop, AgentLoopConfig
from core.context import TaskContext
from core.context_manager import UserDynamics, build_context
from core.db import user_transaction
from core.dialog_store import (
    MessageDraft,
    load_dialog,
    save_messages,
    save_summary,
    to_llm_messages,
)
from core.embeddings import EmbeddingsProvider, EmbeddingsUnavailableError
from core.envelope import UpdateEnvelope
from core.limits import (
    LimitAxis,
    LimitsRedis,
    add_message,
    claim_limit_notice,
    limits_snapshot,
    message_limit_reached,
)
from core.llm import LLMMessage, LLMProvider
from core.prompt import PROMPT_VERSION
from core.queue import QueueName
from core.spend import (
    BudgetState,
    SpendRedis,
    add_spend,
    budget_state,
    daily_budget_rub,
    get_spent_rub,
)
from core.summarize import summarize_tail
from core.tokens import count_tokens
from core.tools_dispatch import ToolDispatcher
from core.usage_recorder import UsageRecord, UsageRecordingProvider
from core.usage_store import save_usage
from worker.app import SendReplyCallback

UNSUPPORTED_CONTENT_TEXT = "Пока я понимаю только текст."
LOGGER = logging.getLogger(__name__)
AUTOINJECT_THRESHOLD = 0.80
AUTOINJECT_LIMIT = 5
SOFT_REFUSE_TEXT = (
    "На сегодня дневной лимит ассистента исчерпан. Продолжим после полуночи — "
    "или напишите /feedback, если лимит мешает"
)
MESSAGE_LIMIT_TEXT = (
    "На сегодня лимит сообщений исчерпан. Продолжим после полуночи — "
    "или напишите /feedback, если лимита не хватает"
)
LIMIT_APPROACH_BUDGET_TEXT = (
    "⚠️ Использовано 80% дневного бюджета ассистента — после превышения возможности "
    "на сегодня будут ограничены"
)


class AgentRedis(SpendRedis, LimitsRedis, Protocol):
    """Redis commands used by the agent budget guard."""

    def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> Awaitable[object]: ...


class AgentProcessor:
    """Turn active-user text updates into a persisted agent task."""

    def __init__(
        self,
        provider: LLMProvider,
        dispatcher: ToolDispatcher,
        agent_config: AgentLoopConfig,
        *,
        app_pool: asyncpg.Pool,
        send: SendReplyCallback,
        logger: logging.Logger | None = None,
        embeddings: EmbeddingsProvider | None = None,
        queue_redis: AgentRedis | None = None,
    ) -> None:
        self._provider = provider
        self._dispatcher = dispatcher
        self._agent_config = agent_config
        self._app_pool = app_pool
        self._send = send
        self._logger = logger if logger is not None else LOGGER
        self._embeddings = embeddings
        self._queue_redis = queue_redis
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        """Process text, retaining the dialogue and relaying optional progress notifications."""

        if envelope.kind not in {"text", "agent_task"}:
            return UNSUPPORTED_CONTENT_TEXT
        text = envelope.payload.get("text")
        if not isinstance(text, str):
            return UNSUPPORTED_CONTENT_TEXT
        if envelope.kind == "text" and await self._message_limit_reached(context):
            return MESSAGE_LIMIT_TEXT
        state = await self._budget_state(context)
        if state is not BudgetState.OK:
            self._logger.info(
                "daily budget degradation active: %s",
                state,
                extra={"trace_id": str(context.trace_id)},
            )
        if envelope.kind == "agent_task" and state is not BudgetState.OK:
            title = envelope.payload.get("title")
            if not isinstance(title, str):
                return UNSUPPORTED_CONTENT_TEXT
            if await self._claim_background_notice(context):
                await self._send(
                    context.chat_id,
                    f"⏰ {title}: фоновая задача пропущена — дневной лимит исчерпан, "
                    "продолжится завтра",
                )
            return None
        if envelope.kind == "text" and state is BudgetState.SOFT_REFUSE:
            return SOFT_REFUSE_TEXT

        history = await load_dialog(self._app_pool, context.user_id)
        facts = await _load_memory_facts(
            self._app_pool,
            context,
            text,
            self._embeddings,
            self._logger,
        )
        built = build_context(
            _dynamics(context.timezone, context.plan),
            facts=facts,
            summary=history.summary,
            tail=to_llm_messages(history.tail),
            lean=envelope.kind == "text" and state is BudgetState.SHORT_CONTEXT,
        )
        messages = [
            built.system_message,
            *built.tail,
            LLMMessage(role="user", content=text),
        ]
        initial_message_count = len(messages)
        recorder = UsageRecordingProvider(self._provider)
        loop = AgentLoop(recorder, self._dispatcher, self._agent_config)

        async def notify_progress(progress_text: str) -> None:
            await self._send(context.chat_id, progress_text)

        if envelope.kind == "text":
            await self._add_message(context)
        result = await loop.run(messages, context, notify_progress=notify_progress)
        reply_text = result.text
        if envelope.kind == "agent_task":
            title = envelope.payload.get("title")
            if not isinstance(title, str):
                return UNSUPPORTED_CONTENT_TEXT
            reply_text = f"⏰ {title}:\n{result.text}"
        user_meta = _voice_meta(envelope.payload)
        drafts = [
            MessageDraft(role="user", content=text, meta=user_meta),
            *[_draft_from_message(message) for message in messages[initial_message_count:]],
            MessageDraft(
                role="assistant",
                content=reply_text,
                meta={"prompt_version": PROMPT_VERSION, "stop_reason": result.stop_reason},
            ),
        ]
        await save_messages(self._app_pool, context.user_id, drafts, context.trace_id)
        await save_usage(
            self._app_pool,
            context.user_id,
            context.trace_id,
            _queue_for_envelope(envelope),
            recorder.records,
        )
        await self._add_recorded_spend(context, recorder.records)
        self._schedule_limit_approach_notifications(envelope, context)

        if built.needs_summarization and built.trimmed:
            upto_message_id = history.tail[len(built.trimmed) - 1].id
            task = asyncio.create_task(
                self._save_trimmed_summary(
                    context.user_id,
                    context.trace_id,
                    context.timezone,
                    built.trimmed,
                    upto_message_id,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return reply_text

    async def _save_trimmed_summary(
        self,
        user_id: UUID,
        trace_id: UUID,
        timezone: str,
        trimmed: list[LLMMessage],
        upto_message_id: UUID,
    ) -> None:
        """Summarize older context in the background without affecting the current reply."""

        try:
            recorder = UsageRecordingProvider(self._provider)
            summary = await summarize_tail(recorder, trimmed)
            await save_summary(
                self._app_pool,
                user_id,
                summary,
                upto_message_id,
                count_tokens(summary),
            )
            await save_usage(
                self._app_pool,
                user_id,
                trace_id,
                "background",
                recorder.records,
            )
            if self._queue_redis is not None and recorder.records:
                await add_spend(
                    self._queue_redis,
                    user_id,
                    timezone,
                    sum((record.cost_usd for record in recorder.records), Decimal(0)),
                )
        except Exception:
            self._logger.exception("dialogue summarization failed")

    async def _budget_state(self, context: TaskContext) -> BudgetState:
        """Read the daily budget once, failing open when Redis is unavailable."""

        if self._queue_redis is None:
            return BudgetState.OK
        spent = await get_spent_rub(self._queue_redis, context.user_id, context.timezone)
        return budget_state(spent, daily_budget_rub(context.plan))

    async def _message_limit_reached(self, context: TaskContext) -> bool:
        """Read the message counter once before budget degradation is evaluated."""

        if self._queue_redis is None:
            return False
        return await message_limit_reached(
            self._queue_redis, context.user_id, context.plan, context.timezone
        )

    async def _add_message(self, context: TaskContext) -> None:
        """Record an interactive message immediately before it enters the agent loop."""

        if self._queue_redis is not None:
            await add_message(self._queue_redis, context.user_id, context.timezone)

    async def _claim_background_notice(self, context: TaskContext) -> bool:
        """Claim the local-day notification slot for a skipped background task."""

        if self._queue_redis is None:
            return False
        current = datetime.now(ZoneInfo(context.timezone))
        tomorrow = datetime.combine(
            current.date() + timedelta(days=1), time.min, tzinfo=current.tzinfo
        )
        seconds_until_midnight = max(1, ceil(tomorrow.timestamp() - current.timestamp()))
        key = f"spend_notice:{context.user_id}:{current:%Y%m%d}"
        try:
            return bool(await self._queue_redis.set(key, "1", nx=True, ex=seconds_until_midnight))
        except Exception:
            self._logger.warning("background budget notice claim failed", exc_info=True)
            return False

    async def _add_recorded_spend(
        self, context: TaskContext, records: Sequence[UsageRecord]
    ) -> None:
        """Account for successfully persisted LLM usage without touching the usage store."""

        if self._queue_redis is None or not records:
            return
        total = sum((record.cost_usd for record in records), Decimal(0))
        await add_spend(self._queue_redis, context.user_id, context.timezone, total)

    def _schedule_limit_approach_notifications(
        self, envelope: UpdateEnvelope, context: TaskContext
    ) -> None:
        """Queue post-reply limit warnings without delaying a completed task."""

        if self._queue_redis is None:
            return
        task = asyncio.create_task(self._notify_limit_approach(envelope, context))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _notify_limit_approach(self, envelope: UpdateEnvelope, context: TaskContext) -> None:
        """Send independent local-day budget and message approach warnings."""

        try:
            if self._queue_redis is None:
                return
            snapshot = await limits_snapshot(
                self._queue_redis, context.user_id, context.plan, context.timezone
            )
            period = datetime.now(ZoneInfo(context.timezone)).strftime("%Y%m%d")
            if (
                envelope.kind in {"text", "agent_task"}
                and snapshot.budget_rub.used >= Decimal("0.8") * snapshot.budget_rub.limit
                and await claim_limit_notice(
                    self._queue_redis, LimitAxis.BUDGET, context.user_id, period
                )
            ):
                await self._send(context.chat_id, LIMIT_APPROACH_BUDGET_TEXT)
            if (
                envelope.kind == "text"
                and snapshot.messages.used >= Decimal("0.8") * snapshot.messages.limit
                and await claim_limit_notice(
                    self._queue_redis, LimitAxis.MESSAGES, context.user_id, period
                )
            ):
                await self._send(
                    context.chat_id,
                    f"⚠️ Использовано {snapshot.messages.used} из {snapshot.messages.limit} "
                    "сообщений на сегодня",
                )
        except Exception:
            self._logger.warning("limit approach notification failed", exc_info=True)


def _dynamics(timezone: str, plan: str) -> UserDynamics:
    current = datetime.now(ZoneInfo(timezone))
    return UserDynamics(
        current_time=current.isoformat(),
        weekday=current.strftime("%A"),
        timezone=timezone,
        plan=plan,
    )


def _draft_from_message(message: LLMMessage) -> MessageDraft:
    if message.role == "system":
        raise ValueError("System messages must not be persisted in dialogue history.")
    return MessageDraft(
        role=message.role,
        content=message.content,
        tool_calls=message.tool_calls,
        tool_call_id=message.tool_call_id,
    )


def _queue_for_envelope(envelope: UpdateEnvelope) -> QueueName:
    """Infer queue attribution for envelopes that do not carry their queue name."""

    if envelope.kind == "text":
        return "interactive"
    return "background"


def _voice_meta(payload: dict[str, object]) -> dict[str, object] | None:
    """Return storage metadata for a transcript that came from a voice message."""

    if payload.get("modality") != "voice":
        return None
    return {"modality": "voice", "duration": payload.get("duration")}


async def _load_memory_facts(
    pool: asyncpg.Pool,
    context: TaskContext,
    text: str,
    embeddings: EmbeddingsProvider | None,
    logger: logging.Logger,
) -> tuple[str, ...]:
    """Retrieve relevant user facts without allowing memory outages to stop a task."""

    if embeddings is None:
        _memory_warning(logger, "memory autoinject skipped: memory disabled")
        return ()
    try:
        vectors = await embeddings.embed([text], "query")
    except EmbeddingsUnavailableError:
        _memory_warning(logger, "memory autoinject skipped: embeddings unavailable")
        return ()
    if len(vectors) != 1:
        _memory_warning(logger, "memory autoinject skipped: invalid embeddings response")
        return ()

    from tools.memory import vector_to_literal

    vector_literal = vector_to_literal(vectors[0])
    async with user_transaction(pool, context.user_id) as connection:
        rows = await connection.fetch(
            """
            SELECT id, text, 1 - (embedding <=> $1::vector) AS sim
            FROM memory_facts
            WHERE 1 - (embedding <=> $1::vector) >= $2
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            vector_literal,
            AUTOINJECT_THRESHOLD,
            AUTOINJECT_LIMIT,
        )
        selected = [row for row in rows if float(row["sim"]) >= AUTOINJECT_THRESHOLD][
            :AUTOINJECT_LIMIT
        ]
        if selected:
            await connection.execute(
                "UPDATE memory_facts SET last_used_at = now() WHERE id = ANY($1::uuid[])",
                [row["id"] for row in selected],
            )
    return tuple(str(row["text"]) for row in selected)


def _memory_warning(logger: logging.Logger, message: str) -> None:
    """Log memory degradation while preserving lightweight test loggers."""

    warning = getattr(logger, "warning", None)
    if callable(warning):
        warning(message)
