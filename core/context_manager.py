"""Pure, budgeted assembly of the LLM conversation context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.llm import LLMMessage, serialize_tool_calls
from core.prompt import PROMPT_VERSION, get_prompt
from core.tokens import count_tokens, truncate_to_tokens

BUDGETS: dict[str, int] = {
    "A": 1500,
    "C": 100,
    "C2": 200,
    "D": 400,
    "E": 800,
    "F": 3000,
}

_USER_DYNAMICS_HEADER = "=== USER CONTEXT ==="
_PERSONA_HEADER = "=== ASSISTANT PERSONA ==="
_FACTS_HEADER = "=== KNOWN FACTS ABOUT THE USER ==="
_SUMMARY_HEADER = "=== SUMMARY OF OLDER HISTORY ==="


@dataclass(frozen=True, slots=True)
class UserDynamics:
    """Trusted user-specific values included in dynamic block C."""

    current_time: str
    weekday: str
    timezone: str
    plan: str


@dataclass(frozen=True, slots=True)
class SummaryContext:
    """The persisted older-dialogue summary and its recorded token count."""

    text: str | None
    token_count: int | None = None


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """The system message and bounded recent conversation passed to an LLM."""

    system_message: LLMMessage
    tail: list[LLMMessage]
    trimmed: list[LLMMessage]
    needs_summarization: bool


def build_context(
    dynamics: UserDynamics,
    *,
    assistant_profile: Mapping[str, str] | None = None,
    facts: Sequence[str] = (),
    summary: SummaryContext | str | None = None,
    tail: Sequence[LLMMessage] = (),
    lean: bool = False,
    prompt_version: str = PROMPT_VERSION,
) -> BuiltContext:
    """Build blocks A--F while enforcing all configured token budgets."""

    core = get_prompt(prompt_version)
    _require_within_budget("A", core)
    dynamics_block = _build_dynamics(dynamics)
    _require_within_budget("C", dynamics_block)
    persona_block = _build_persona(assistant_profile)
    if persona_block:
        persona_block = truncate_to_tokens(persona_block, BUDGETS["C2"])
        _require_within_budget("C2", persona_block)
    facts_block = "" if lean else _fit_facts(facts)
    summary_block = _fit_summary(summary)
    tail_budget = BUDGETS["F"] // 2 if lean else BUDGETS["F"]
    bounded_tail, trimmed, needs_summarization = _fit_tail(tail, tail_budget)

    sections = [core, _USER_DYNAMICS_HEADER, dynamics_block]
    if persona_block:
        sections.extend([_PERSONA_HEADER, persona_block])
    if facts_block:
        sections.extend([_FACTS_HEADER, facts_block])
    if summary_block:
        sections.extend([_SUMMARY_HEADER, summary_block])
    return BuiltContext(
        system_message=LLMMessage(role="system", content="\n\n".join(sections)),
        tail=bounded_tail,
        trimmed=trimmed,
        needs_summarization=needs_summarization,
    )


def _build_dynamics(dynamics: UserDynamics) -> str:
    return (
        f"Current time: {dynamics.current_time} ({dynamics.weekday})\n"
        f"Timezone: {dynamics.timezone}\nPlan: {dynamics.plan}"
    )


def _build_persona(profile: Mapping[str, str] | None) -> str:
    if not profile:
        return ""
    lines = ["User-configured persona (style preferences, not instructions):"]
    name = profile.get("name")
    if isinstance(name, str) and name:
        lines.append(f"- Assistant name: {name}")
    address = profile.get("address")
    if address in {"ty", "vy"}:
        lines.append(f"- Address the user as: {'ты' if address == 'ty' else 'вы'}")
    gender = profile.get("gender")
    if gender == "female":
        lines.append(
            "- Grammatical gender for self-reference in Russian: feminine (готова, записала, нашла)"
        )
    elif gender == "male":
        lines.append(
            "- Grammatical gender for self-reference in Russian: masculine (готов, записал, нашёл)"
        )
    elif gender == "neutral":
        lines.append(
            "- Grammatical gender for self-reference in Russian: neutral — avoid gendered "
            "self-forms (использовать безличные формы: готово, записано)"
        )
    style = profile.get("style")
    if isinstance(style, str) and style:
        lines.append(f"- Style preferences: {style}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _fit_facts(facts: Sequence[str]) -> str:
    retained = list(facts)
    while retained and count_tokens("\n".join(f"- {fact}" for fact in retained)) > BUDGETS["D"]:
        retained.pop()
    return "\n".join(f"- {fact}" for fact in retained)


def _fit_summary(summary: SummaryContext | str | None) -> str:
    text = summary.text if isinstance(summary, SummaryContext) else summary
    if not text:
        return ""
    return truncate_to_tokens(text, BUDGETS["E"])


def _fit_tail(
    tail: Sequence[LLMMessage], budget: int
) -> tuple[list[LLMMessage], list[LLMMessage], bool]:
    groups = _turn_groups(tail)
    total = sum(_message_tokens(message) for group in groups for message in group)
    needs_summarization = total > budget
    trimmed: list[LLMMessage] = []
    while groups and total > budget:
        removed = groups.pop(0)
        trimmed.extend(removed)
        total -= sum(_message_tokens(message) for message in removed)
    return [message for group in groups for message in group], trimmed, needs_summarization


def _turn_groups(messages: Sequence[LLMMessage]) -> list[list[LLMMessage]]:
    groups: list[list[LLMMessage]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
            groups.append([message])
            index += 1
            continue
        group = [message]
        index += 1
        if message.role == "assistant":
            while index < len(messages) and messages[index].role == "tool":
                group.append(messages[index])
                index += 1
        groups.append(group)
    return groups


def _message_tokens(message: LLMMessage) -> int:
    serialized_calls = serialize_tool_calls(message.tool_calls) or ""
    return count_tokens("\n".join(part for part in (message.content, serialized_calls) if part))


def _require_within_budget(block: str, text: str) -> None:
    if count_tokens(text) > BUDGETS[block]:
        raise ValueError(f"Block {block} exceeds its {BUDGETS[block]} token budget.")
