"""Worker adapter that adds trusted context and dialogue persistence to the agent loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from core.agent import AgentLoop
from core.context import TaskContext
from core.context_manager import UserDynamics, build_context
from core.dialog_store import (
    MessageDraft,
    load_dialog,
    save_messages,
    save_summary,
    to_llm_messages,
)
from core.envelope import UpdateEnvelope
from core.llm import LLMMessage, LLMProvider
from core.prompt import PROMPT_VERSION
from core.summarize import summarize_tail
from core.tokens import count_tokens
from worker.app import SendReplyCallback

UNSUPPORTED_CONTENT_TEXT = "Пока я понимаю только текст."
LOGGER = logging.getLogger(__name__)


class AgentProcessor:
    """Turn active-user text updates into a persisted agent task."""

    def __init__(
        self,
        loop: AgentLoop,
        *,
        app_pool: asyncpg.Pool,
        summarizer: LLMProvider,
        send: SendReplyCallback,
        logger: logging.Logger | None = None,
    ) -> None:
        self._loop = loop
        self._app_pool = app_pool
        self._summarizer = summarizer
        self._send = send
        self._logger = logger if logger is not None else LOGGER
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        """Process text, retaining the dialogue and relaying optional progress notifications."""

        if envelope.kind != "text":
            return UNSUPPORTED_CONTENT_TEXT
        text = envelope.payload.get("text")
        if not isinstance(text, str):
            return UNSUPPORTED_CONTENT_TEXT

        history = await load_dialog(self._app_pool, context.user_id)
        built = build_context(
            _dynamics(context.timezone),
            facts=(),
            summary=history.summary,
            tail=to_llm_messages(history.tail),
        )
        messages = [
            built.system_message,
            *built.tail,
            LLMMessage(role="user", content=text),
        ]
        initial_message_count = len(messages)

        async def notify_progress(progress_text: str) -> None:
            await self._send(context.chat_id, progress_text)

        result = await self._loop.run(messages, context, notify_progress=notify_progress)
        drafts = [
            MessageDraft(role="user", content=text),
            *[_draft_from_message(message) for message in messages[initial_message_count:]],
            MessageDraft(
                role="assistant",
                content=result.text,
                meta={"prompt_version": PROMPT_VERSION, "stop_reason": result.stop_reason},
            ),
        ]
        await save_messages(self._app_pool, context.user_id, drafts, context.trace_id)

        if built.needs_summarization and built.trimmed:
            upto_message_id = history.tail[len(built.trimmed) - 1].id
            task = asyncio.create_task(
                self._save_trimmed_summary(context.user_id, built.trimmed, upto_message_id)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return result.text

    async def _save_trimmed_summary(
        self,
        user_id: UUID,
        trimmed: list[LLMMessage],
        upto_message_id: UUID,
    ) -> None:
        """Summarize older context in the background without affecting the current reply."""

        try:
            summary = await summarize_tail(self._summarizer, trimmed)
            await save_summary(
                self._app_pool,
                user_id,
                summary,
                upto_message_id,
                count_tokens(summary),
            )
        except Exception:
            self._logger.exception("dialogue summarization failed")


def _dynamics(timezone: str) -> UserDynamics:
    current = datetime.now(ZoneInfo(timezone))
    return UserDynamics(
        current_time=current.isoformat(),
        weekday=current.strftime("%A"),
        timezone=timezone,
        plan="standard",
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
