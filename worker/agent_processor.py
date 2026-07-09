"""Worker adapter for the provider-neutral agent loop."""

from __future__ import annotations

from core.agent import AgentLoop
from core.context import TaskContext
from core.envelope import UpdateEnvelope
from core.llm import LLMMessage
from worker.app import SendReplyCallback

SYSTEM_PROMPT_DRAFT = "Ты полезный персональный ассистент. Отвечай по-русски."
UNSUPPORTED_CONTENT_TEXT = "Пока я понимаю только текст."


class AgentProcessor:
    """Turn active-user text updates into one agent task."""

    def __init__(self, loop: AgentLoop, *, send: SendReplyCallback) -> None:
        self._loop = loop
        self._send = send

    async def process(self, envelope: UpdateEnvelope, context: TaskContext) -> str | None:
        """Process text only, relaying one optional progress notification."""

        if envelope.kind != "text":
            return UNSUPPORTED_CONTENT_TEXT
        text = envelope.payload.get("text")
        if not isinstance(text, str):
            return UNSUPPORTED_CONTENT_TEXT

        async def notify_progress(progress_text: str) -> None:
            await self._send(context.chat_id, progress_text)

        result = await self._loop.run(
            [
                LLMMessage(role="system", content=SYSTEM_PROMPT_DRAFT),
                LLMMessage(role="user", content=text),
            ],
            context,
            notify_progress=notify_progress,
        )
        return result.text
