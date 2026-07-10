"""Tests for LLM-backed summary generation."""

from __future__ import annotations

from core.context_manager import BUDGETS
from core.llm import LLMMessage
from core.llm_mock import MockLLMProvider, mock_text_response
from core.summarize import summarize_tail
from core.tokens import count_tokens


async def test_summarize_tail_passes_tail_content_to_llm() -> None:
    llm = MockLLMProvider.scripted([mock_text_response("Краткое резюме")])
    await summarize_tail(llm, [LLMMessage(role="user", content="Я люблю чай")])

    assert any(message.content == "Я люблю чай" for message in llm.calls[0].messages)


async def test_summarize_tail_truncates_over_budget_response() -> None:
    llm = MockLLMProvider.scripted([mock_text_response("word " * 2_000)])
    summary = await summarize_tail(llm, [LLMMessage(role="user", content="context")])

    assert count_tokens(summary) <= BUDGETS["E"]
