"""Unit tests for the provider-neutral LLM layer."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

import core.llm_openrouter as llm_openrouter
from core.llm import LLMMessage, LLMProviderError, LLMRateLimitError, LLMServerError, ToolDefinition
from core.llm_mock import MockLLMProvider, mock_text_response, mock_tool_call_response
from core.llm_openrouter import OpenRouterProvider, OpenRouterSettings


class FakeCompletions:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.completions = FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


def _provider(client: FakeClient, *, allowed_providers: tuple[str, ...] = ()) -> OpenRouterProvider:
    return OpenRouterProvider(
        OpenRouterSettings(
            model="test/model",
            allowed_providers=allowed_providers,
            request_timeout_seconds=12.0,
        ),
        client=client,
    )


def _response(
    *,
    content: str | None = "answer",
    tool_calls: list[object] | None = None,
    cost: object | None = None,
    cached_tokens: int | None = None,
) -> object:
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    if cost is not None:
        usage.cost = cost
    if cached_tokens is not None:
        usage.prompt_tokens_details = SimpleNamespace(cached_tokens=cached_tokens)
    return SimpleNamespace(
        model="served/model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason="stop" if tool_calls is None else "tool_calls",
            )
        ],
        usage=usage,
    )


async def test_openrouter_builds_private_allowlisted_request() -> None:
    client = FakeClient([_response()])
    provider = _provider(client, allowed_providers=("Provider A", "Provider B"))
    messages = [
        LLMMessage(role="system", content="You help."),
        LLMMessage(role="user", content="Hello"),
    ]
    tools = [
        ToolDefinition(
            name="lookup",
            description="Find a value.",
            parameters={"type": "object", "properties": {}},
        )
    ]

    await provider.generate(messages, tools=tools, temperature=0.2, max_tokens=123)

    request = client.completions.requests[0]
    assert request["model"] == "test/model"
    assert request["messages"] == [
        {"role": "system", "content": "You help."},
        {"role": "user", "content": "Hello"},
    ]
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Find a value.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert request["temperature"] == 0.2
    assert request["max_tokens"] == 123
    assert request["extra_body"] == {
        "provider": {
            "data_collection": "deny",
            "allow_fallbacks": False,
            "order": ["Provider A", "Provider B"],
        },
        "usage": {"include": True},
    }


async def test_openrouter_omits_empty_provider_order() -> None:
    client = FakeClient([_response()])

    await _provider(client).generate([LLMMessage(role="user", content="Hello")])

    extra_body = cast("dict[str, object]", client.completions.requests[0]["extra_body"])
    provider = cast("dict[str, object]", extra_body["provider"])
    assert provider == {"data_collection": "deny", "allow_fallbacks": False}
    assert extra_body["usage"] == {"include": True}


async def test_openrouter_maps_text_usage_cached_tokens_and_cost() -> None:
    client = FakeClient([_response(content="Done", cost="0.00125", cached_tokens=4)])

    result = await _provider(client).generate([LLMMessage(role="user", content="Hello")])

    assert result.message == LLMMessage(role="assistant", content="Done")
    assert result.model == "served/model"
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7
    assert result.usage.cached_prompt_tokens == 4
    assert result.usage.cost_usd == Decimal("0.00125")


async def test_openrouter_maps_tool_calls_and_missing_cost() -> None:
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="lookup", arguments='{"query":"tea"}'),
    )
    client = FakeClient([_response(content=None, tool_calls=[tool_call])])

    result = await _provider(client).generate([LLMMessage(role="user", content="Hello")])

    assert result.message.tool_calls is not None
    assert result.message.tool_calls[0].name == "lookup"
    assert result.message.tool_calls[0].arguments_json == '{"query":"tea"}'
    assert result.usage.cached_prompt_tokens == 0
    assert result.usage.cost_usd is None


async def test_openrouter_maps_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRateLimitError(Exception):
        pass

    monkeypatch.setattr(llm_openrouter, "RateLimitError", FakeRateLimitError)
    client = FakeClient([FakeRateLimitError("too many requests")])

    with pytest.raises(LLMRateLimitError, match="too many requests"):
        await _provider(client).generate([LLMMessage(role="user", content="Hello")])


async def test_openrouter_maps_server_and_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAPIStatusError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            super().__init__(f"status {status_code}")

    monkeypatch.setattr(llm_openrouter, "APIStatusError", FakeAPIStatusError)
    server_client = FakeClient([FakeAPIStatusError(503)])
    other_client = FakeClient([RuntimeError("connection failed")])

    with pytest.raises(LLMServerError, match="status 503"):
        await _provider(server_client).generate([LLMMessage(role="user", content="Hello")])
    with pytest.raises(LLMProviderError, match="connection failed"):
        await _provider(other_client).generate([LLMMessage(role="user", content="Hello")])


async def test_mock_provider_returns_scripted_responses_and_records_calls() -> None:
    provider = MockLLMProvider.scripted(
        [mock_tool_call_response("lookup", '{"query":"tea"}'), mock_text_response("Done")]
    )
    tool = ToolDefinition(name="lookup", description="Find a value.", parameters={"type": "object"})

    first = await provider.generate(
        [LLMMessage(role="user", content="Find tea")],
        tools=[tool],
        temperature=0.1,
        max_tokens=50,
    )
    second = await provider.generate([LLMMessage(role="tool", content="results", tool_call_id="x")])

    assert first.message.tool_calls is not None
    assert first.message.tool_calls[0].arguments_json == '{"query":"tea"}'
    assert first.usage.prompt_tokens == 10
    assert first.usage.completion_tokens == 5
    assert first.usage.cost_usd is None
    assert second.message.content == "Done"
    assert provider.calls[0].messages == (LLMMessage(role="user", content="Find tea"),)
    assert provider.calls[0].tools == (tool,)
    assert provider.calls[0].kwargs == {"temperature": 0.1, "max_tokens": 50}


async def test_mock_provider_raises_when_script_is_exhausted() -> None:
    provider = MockLLMProvider.scripted([])

    with pytest.raises(AssertionError, match="script is exhausted"):
        await provider.generate([LLMMessage(role="user", content="Hello")])
