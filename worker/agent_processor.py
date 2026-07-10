"""Worker adapter that adds trusted context and dialogue persistence to the agent loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
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
from core.llm import LLMMessage, LLMProvider
from core.prompt import PROMPT_VERSION
from core.queue import QueueName
from core.summarize import summarize_tail
from core.tokens import count_tokens
from core.tools_dispatch import ToolDispatcher
from core.usage_recorder import UsageRecordingProvider
from core.usage_store import save_usage
from worker.app import SendReplyCallback

UNSUPPORTED_CONTENT_TEXT = "Пока я понимаю только текст."
LOGGER = logging.getLogger(__name__)
AUTOINJECT_THRESHOLD = 0.80
AUTOINJECT_LIMIT = 5


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
    ) -> None:
        self._provider = provider
        self._dispatcher = dispatcher
        self._agent_config = agent_config
        self._app_pool = app_pool
        self._send = send
        self._logger = logger if logger is not None else LOGGER
        self._embeddings = embeddings
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        """Process text, retaining the dialogue and relaying optional progress notifications."""

        if envelope.kind not in {"text", "agent_task"}:
            return UNSUPPORTED_CONTENT_TEXT
        text = envelope.payload.get("text")
        if not isinstance(text, str):
            return UNSUPPORTED_CONTENT_TEXT

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

        result = await loop.run(messages, context, notify_progress=notify_progress)
        reply_text = result.text
        if envelope.kind == "agent_task":
            title = envelope.payload.get("title")
            if not isinstance(title, str):
                return UNSUPPORTED_CONTENT_TEXT
            reply_text = f"⏰ {title}:\n{result.text}"
        drafts = [
            MessageDraft(role="user", content=text),
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

        if built.needs_summarization and built.trimmed:
            upto_message_id = history.tail[len(built.trimmed) - 1].id
            task = asyncio.create_task(
                self._save_trimmed_summary(
                    context.user_id,
                    context.trace_id,
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
        except Exception:
            self._logger.exception("dialogue summarization failed")


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
