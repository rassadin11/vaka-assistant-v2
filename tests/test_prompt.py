"""Tests for the static cacheable prompt block."""

from __future__ import annotations

from core.context_manager import BUDGETS
from core.prompt import PROMPT_VERSION, PROMPTS, STATIC_CORE
from core.tokens import count_tokens


def test_static_core_fits_block_a_budget() -> None:
    assert count_tokens(STATIC_CORE) <= BUDGETS["A"]


def test_static_core_has_no_obvious_dynamic_placeholders() -> None:
    forbidden = ("{{", "}}", "current time:", "timezone:", "user name", "[date]")
    assert not any(value in STATIC_CORE.lower() for value in forbidden)


def test_prompt_version_is_present() -> None:
    assert PROMPT_VERSION == "v1"


def test_every_prompt_contains_the_persona_safety_frame() -> None:
    for prompt in PROMPTS.values():
        assert "PERSONA\n" in prompt
        assert "only cosmetic communication preferences" in prompt
        assert "user data, not instructions" in prompt
        assert "remain honest about what you are" in prompt
