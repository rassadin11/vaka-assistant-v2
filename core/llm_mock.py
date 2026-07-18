"""Deterministic LLM provider for unit tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.llm import LLMMessage, LLMResponse, LLMToolCall, LLMUsage, ToolDefinition

_DEFAULT_MODEL = "mock-model"


@dataclass(frozen=True, slots=True)
class MockLLMCall:
    """A recorded call made to :class:`MockLLMProvider`."""

    messages: tuple[LLMMessage, ...]
    tools: tuple[ToolDefinition, ...] | None
    temperature: float | None
    max_tokens: int | None

    @property
    def kwargs(self) -> dict[str, float | int | None]:
        """Return keyword arguments in the shape accepted by ``generate``."""

        return {"temperature": self.temperature, "max_tokens": self.max_tokens}


class MockLLMProvider:
    """Return scripted responses while recording every request."""

    def __init__(self, responses: Sequence[LLMResponse | BaseException]) -> None:
        self._responses = list(responses)
        self.calls: list[MockLLMCall] = []

    @classmethod
    def scripted(cls, responses: Sequence[LLMResponse | BaseException]) -> MockLLMProvider:
        """Create a mock provider from a response sequence."""

        return cls(responses)

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Record a request and return its next scripted response."""

        self.calls.append(
            MockLLMCall(
                messages=tuple(messages),
                tools=None if tools is None else tuple(tools),
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )
        if not self._responses:
            raise AssertionError("MockLLMProvider script is exhausted.")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def mock_text_response(
    text: str, *, model: str = _DEFAULT_MODEL, finish_reason: str = "stop"
) -> LLMResponse:
    """Build a deterministic text response for a mock script."""

    return LLMResponse(
        message=LLMMessage(role="assistant", content=text),
        usage=_default_usage(),
        model=model,
        finish_reason=finish_reason,
    )


def mock_tool_call_response(
    name: str,
    arguments_json: str,
    *,
    model: str = _DEFAULT_MODEL,
    tool_call_id: str = "mock-tool-call-1",
) -> LLMResponse:
    """Build a deterministic single-tool-call response for a mock script."""

    return LLMResponse(
        message=LLMMessage(
            role="assistant",
            content=None,
            tool_calls=[
                LLMToolCall(id=tool_call_id, name=name, arguments_json=arguments_json),
            ],
        ),
        usage=_default_usage(),
        model=model,
        finish_reason="tool_calls",
    )


def _default_usage() -> LLMUsage:
    return LLMUsage(prompt_tokens=10, completion_tokens=5, cached_prompt_tokens=0)
