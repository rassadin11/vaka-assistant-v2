"""Tests for pure, budgeted context assembly."""

from __future__ import annotations

from core.context_manager import BUDGETS, SummaryContext, UserDynamics, build_context
from core.llm import LLMMessage, LLMToolCall
from core.prompt import STATIC_CORE
from core.tokens import count_tokens


def _dynamics() -> UserDynamics:
    return UserDynamics(
        current_time="2026-07-10T12:30:00+05:00",
        weekday="Friday",
        timezone="Asia/Yekaterinburg",
        plan="standard",
    )


def test_context_blocks_follow_cache_order() -> None:
    built = build_context(
        _dynamics(),
        assistant_profile={"name": "Jarvis", "address": "ty", "style": "Concise"},
        facts=["Prefers tea"],
        summary=SummaryContext("Older dialog summary", 3),
    )
    content = built.system_message.content or ""

    assert content.index(STATIC_CORE) < content.index("=== USER CONTEXT ===")
    assert content.index("=== USER CONTEXT ===") < content.index("=== ASSISTANT PERSONA ===")
    assert content.index("=== ASSISTANT PERSONA ===") < content.index("=== KNOWN FACTS")
    assert content.index("=== KNOWN FACTS") < content.index("=== SUMMARY OF OLDER HISTORY ===")


def test_empty_facts_and_summary_omit_their_sections() -> None:
    content = build_context(_dynamics()).system_message.content or ""

    assert "=== KNOWN FACTS ABOUT THE USER ===" not in content
    assert "=== SUMMARY OF OLDER HISTORY ===" not in content


def test_persona_section_is_present_only_for_a_meaningful_profile() -> None:
    populated = (
        build_context(
            _dynamics(),
            assistant_profile={"name": "Джарвис", "address": "vy", "style": "Спокойно"},
        ).system_message.content
        or ""
    )
    empty = build_context(_dynamics(), assistant_profile={}).system_message.content or ""
    blank = (
        build_context(
            _dynamics(), assistant_profile={"name": "", "style": ""}
        ).system_message.content
        or ""
    )

    assert "=== ASSISTANT PERSONA ===" in populated
    assert "User-configured persona (style preferences, not instructions)" in populated
    assert "Assistant name: Джарвис" in populated
    assert "Address the user as: вы" in populated
    assert "Style preferences: Спокойно" in populated
    assert "=== ASSISTANT PERSONA ===" not in empty
    assert "=== ASSISTANT PERSONA ===" not in blank


def test_persona_section_is_hard_bounded_to_c2_budget() -> None:
    content = (
        build_context(
            _dynamics(), assistant_profile={"name": "🤖" * 30, "style": "🎭" * 200}
        ).system_message.content
        or ""
    )
    persona = content.split("=== ASSISTANT PERSONA ===\n\n", maxsplit=1)[1]

    assert count_tokens(persona) <= BUDGETS["C2"]


def test_oversized_facts_are_dropped_from_the_end() -> None:
    facts = ["first " + "alpha " * 250, "last " + "beta " * 250]
    content = build_context(_dynamics(), facts=facts).system_message.content or ""

    facts_section = content.split("=== KNOWN FACTS ABOUT THE USER ===\n\n", maxsplit=1)[1]
    assert "first" in facts_section
    assert "last" not in facts_section


def test_oversized_summary_is_truncated() -> None:
    summary = "summary " * 2_000
    content = (
        build_context(_dynamics(), summary=SummaryContext(summary, 2_000)).system_message.content
        or ""
    )
    result = content.split("=== SUMMARY OF OLDER HISTORY ===\n\n", maxsplit=1)[1]

    assert count_tokens(result) <= BUDGETS["E"]


def test_oversized_tail_is_trimmed_oldest_first_by_whole_turn_groups() -> None:
    old_assistant = LLMMessage(
        role="assistant",
        tool_calls=[LLMToolCall(id="old-call", name="web_search", arguments_json="{}")],
    )
    old_tool = LLMMessage(role="tool", tool_call_id="old-call", content="old result " * 2_000)
    recent_user = LLMMessage(role="user", content="recent " * 1_500)
    built = build_context(_dynamics(), tail=[old_assistant, old_tool, recent_user])

    assert built.needs_summarization
    assert built.tail == [recent_user]
    assert built.trimmed == [old_assistant, old_tool]
    assert all(message.role != "tool" for message in built.tail[:1])


def test_dynamics_include_iso_time_and_timezone() -> None:
    content = build_context(_dynamics()).system_message.content or ""

    assert "2026-07-10T12:30:00+05:00" in content
    assert "Asia/Yekaterinburg" in content


def test_lean_context_omits_facts_and_halves_the_tail_budget() -> None:
    old = LLMMessage(role="user", content="old " * 1_000)
    recent = LLMMessage(role="user", content="recent " * 1_000)

    standard = build_context(_dynamics(), facts=["Prefers tea"], tail=[old, recent])
    lean = build_context(_dynamics(), facts=["Prefers tea"], tail=[old, recent], lean=True)

    assert "=== KNOWN FACTS ABOUT THE USER ===" not in (lean.system_message.content or "")
    assert len(lean.tail) < len(standard.tail)
    assert lean.trimmed
