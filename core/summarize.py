"""LLM-assisted summarization of older dialogue turns."""

from __future__ import annotations

from collections.abc import Sequence

from core.context_manager import BUDGETS
from core.llm import LLMMessage, LLMProvider
from core.tokens import count_tokens, truncate_to_tokens

_SUMMARIZATION_PROMPT = (
    "Summarize the older dialogue for a future assistant turn. Preserve durable facts, open "
    "tasks, user preferences, decisions, and unresolved questions. Do not invent details. "
    "Write the summary in the language of the conversation. Keep it concise and within 800 "
    "tokens."
)


async def summarize_tail(llm: LLMProvider, messages: Sequence[LLMMessage]) -> str:
    """Ask *llm* once for a bounded summary of the supplied dialogue messages."""

    response = await llm.generate(
        [LLMMessage(role="system", content=_SUMMARIZATION_PROMPT), *messages],
        max_tokens=BUDGETS["E"],
    )
    summary = response.message.content or ""
    if count_tokens(summary) > BUDGETS["E"]:
        return truncate_to_tokens(summary, BUDGETS["E"])
    return summary
