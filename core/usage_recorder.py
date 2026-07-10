"""Per-task collection of successful LLM request usage."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from core.llm import LLMMessage, LLMProvider, LLMResponse, ToolDefinition


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """The billable usage reported by one successful provider request."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: Decimal


class UsageRecordingProvider:
    """Record each successful generation while delegating to another provider."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.records: list[UsageRecord] = []

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Generate once and retain usage only after a successful response."""

        response = await self._inner.generate(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = response.usage
        self.records.append(
            UsageRecord(
                model=response.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cached_tokens=usage.cached_prompt_tokens,
                cost_usd=usage.cost_usd or Decimal(0),
            )
        )
        return response
