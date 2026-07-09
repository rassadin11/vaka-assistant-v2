"""OpenRouter implementation of the provider-neutral LLM interface."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast

from openai import APIStatusError, AsyncOpenAI, RateLimitError

from core.llm import (
    LLMMessage,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponse,
    LLMServerError,
    LLMToolCall,
    LLMUsage,
    ToolDefinition,
)
from core.secrets import EnvSecretsProvider, SecretsProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-chat"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class OpenRouterSettings:
    """OpenRouter request settings loaded from environment variables."""

    model: str = DEFAULT_OPENROUTER_MODEL
    allowed_providers: tuple[str, ...] = ()
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS


class _ChatCompletions(Protocol):
    def create(self, **kwargs: object) -> Awaitable[object]:
        """Create a chat completion."""


class _Chat(Protocol):
    completions: _ChatCompletions


class OpenRouterClient(Protocol):
    """Duck-typed subset of ``AsyncOpenAI`` used by this provider."""

    chat: _Chat


def openrouter_settings_from_env() -> OpenRouterSettings:
    """Load OpenRouter settings from environment variables."""

    raw_providers = os.getenv("OPENROUTER_ALLOWED_PROVIDERS", "")
    allowed_providers = tuple(
        provider.strip() for provider in raw_providers.split(",") if provider.strip()
    )
    return OpenRouterSettings(
        model=os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        allowed_providers=allowed_providers,
        request_timeout_seconds=float(
            os.getenv("OPENROUTER_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_REQUEST_TIMEOUT_SECONDS))
        ),
    )


class OpenRouterProvider:
    """Generate LLM responses through OpenRouter's OpenAI-compatible API."""

    def __init__(
        self,
        settings: OpenRouterSettings | None = None,
        *,
        secrets: SecretsProvider | None = None,
        client: OpenRouterClient | None = None,
    ) -> None:
        self._settings = settings if settings is not None else openrouter_settings_from_env()
        if client is None:
            provider = secrets if secrets is not None else EnvSecretsProvider()
            client = cast(
                OpenRouterClient,
                AsyncOpenAI(
                    base_url=OPENROUTER_BASE_URL,
                    api_key=provider.get("OPENROUTER_API_KEY"),
                    timeout=self._settings.request_timeout_seconds,
                ),
            )
        self._client = client

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Create a single OpenRouter chat completion without retrying it."""

        try:
            response = await self._client.chat.completions.create(
                **_build_request(
                    self._settings,
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            )
        except RateLimitError as exc:
            raise LLMRateLimitError(str(exc)) from exc
        except APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMServerError(str(exc)) from exc
            raise LLMProviderError(str(exc)) from exc
        except Exception as exc:
            raise LLMProviderError(str(exc)) from exc

        return _map_response(response)


def _build_request(
    settings: OpenRouterSettings,
    messages: Sequence[LLMMessage],
    *,
    tools: Sequence[ToolDefinition] | None,
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, object]:
    provider: dict[str, object] = {
        "data_collection": "deny",
        "allow_fallbacks": False,
    }
    if settings.allowed_providers:
        provider["order"] = list(settings.allowed_providers)

    request: dict[str, object] = {
        "model": settings.model,
        "messages": [_message_to_wire(message) for message in messages],
        "extra_body": {
            "provider": provider,
            "usage": {"include": True},
        },
    }
    if tools is not None:
        request["tools"] = [_tool_to_wire(tool) for tool in tools]
    if temperature is not None:
        request["temperature"] = temperature
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    return request


def _message_to_wire(message: LLMMessage) -> dict[str, object]:
    wire: dict[str, object] = {"role": message.role, "content": message.content}
    if message.tool_calls is not None:
        wire["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": tool_call.arguments_json,
                },
            }
            for tool_call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    return wire


def _tool_to_wire(tool: ToolDefinition) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _map_response(response: object) -> LLMResponse:
    choices = _required_value(response, "choices")
    if not isinstance(choices, Sequence) or isinstance(choices, str | bytes) or not choices:
        raise LLMProviderError("OpenRouter response did not include a completion choice.")
    choice = choices[0]
    message = _required_value(choice, "message")
    usage = _required_value(response, "usage")

    return LLMResponse(
        message=LLMMessage(
            role="assistant",
            content=_optional_string(message, "content"),
            tool_calls=_map_tool_calls(_optional_value(message, "tool_calls")),
        ),
        usage=LLMUsage(
            prompt_tokens=_integer_or_zero(usage, "prompt_tokens"),
            completion_tokens=_integer_or_zero(usage, "completion_tokens"),
            cached_prompt_tokens=_cached_prompt_tokens(usage),
            cost_usd=_cost(usage),
        ),
        model=_required_string(response, "model"),
        finish_reason=_optional_string(choice, "finish_reason"),
    )


def _map_tool_calls(raw_tool_calls: object | None) -> list[LLMToolCall] | None:
    if raw_tool_calls is None:
        return None
    if not isinstance(raw_tool_calls, Sequence) or isinstance(raw_tool_calls, str | bytes):
        raise LLMProviderError("OpenRouter response tool_calls was not a sequence.")
    return [
        LLMToolCall(
            id=_required_string(tool_call, "id"),
            name=_required_string(_required_value(tool_call, "function"), "name"),
            arguments_json=_required_string(_required_value(tool_call, "function"), "arguments"),
        )
        for tool_call in raw_tool_calls
    ]


def _cached_prompt_tokens(usage: object) -> int:
    details = _optional_value(usage, "prompt_tokens_details")
    if details is None:
        return 0
    return _integer_or_zero(details, "cached_tokens")


def _cost(usage: object) -> Decimal | None:
    raw_cost = _optional_value(usage, "cost")
    if raw_cost is None:
        return None
    try:
        return Decimal(str(raw_cost))
    except ArithmeticError as exc:
        raise LLMProviderError("OpenRouter response cost was not numeric.") from exc


def _integer_or_zero(value: object, name: str) -> int:
    raw_value = _optional_value(value, name)
    return 0 if raw_value is None else int(cast("int | str", raw_value))


def _required_string(value: object, name: str) -> str:
    result = _required_value(value, name)
    if not isinstance(result, str):
        raise LLMProviderError(f"OpenRouter response {name} was not a string.")
    return result


def _optional_string(value: object, name: str) -> str | None:
    result = _optional_value(value, name)
    if result is not None and not isinstance(result, str):
        raise LLMProviderError(f"OpenRouter response {name} was not a string.")
    return result


def _required_value(value: object, name: str) -> object:
    result = _optional_value(value, name)
    if result is None:
        raise LLMProviderError(f"OpenRouter response did not include {name}.")
    return result


def _optional_value(value: object, name: str) -> object | None:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
