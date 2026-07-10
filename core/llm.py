"""Provider-neutral contracts for LLM generation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

LLMRole = Literal["system", "user", "assistant", "tool"]


class LLMToolCall(BaseModel):
    """A tool invocation returned by an LLM without interpreting its arguments."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments_json: str


class LLMMessage(BaseModel):
    """A provider-neutral chat message."""

    model_config = ConfigDict(extra="forbid")

    role: LLMRole
    content: str | None = None
    tool_calls: list[LLMToolCall] | None = None
    tool_call_id: str | None = None


def serialize_tool_calls(tool_calls: Sequence[LLMToolCall] | None) -> str | None:
    """Return a canonical JSON representation suitable for storage and token counting."""

    if not tool_calls:
        return None
    return json.dumps(
        [call.model_dump(mode="json") for call in tool_calls],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class ToolDefinition(BaseModel):
    """Minimal function-tool definition passed to an LLM provider."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters: dict[str, Any]


class LLMUsage(BaseModel):
    """Token and optional cost accounting returned by a provider."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int = 0
    cost_usd: Decimal | None = None


class LLMResponse(BaseModel):
    """A completed LLM generation response."""

    model_config = ConfigDict(extra="forbid")

    message: LLMMessage
    usage: LLMUsage
    model: str
    finish_reason: str | None = None

    @field_validator("message")
    @classmethod
    def _require_assistant_message(cls, value: LLMMessage) -> LLMMessage:
        if value.role != "assistant":
            raise ValueError("LLM responses must contain an assistant message.")
        return value


class LLMProvider(Protocol):
    """Interface used by the agent core for LLM generation."""

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Generate the next assistant message."""


class LLMProviderError(RuntimeError):
    """Raised when an LLM provider cannot complete a request."""


class LLMRateLimitError(LLMProviderError):
    """Raised when an LLM provider responds with HTTP 429."""


class LLMServerError(LLMProviderError):
    """Raised when an LLM provider responds with an HTTP 5xx error."""
