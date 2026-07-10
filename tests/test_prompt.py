"""Tests for the static cacheable prompt block."""

from __future__ import annotations

from core.context_manager import BUDGETS
from core.prompt import PROMPT_VERSION, STATIC_CORE
from core.tokens import count_tokens


def test_static_core_fits_block_a_budget() -> None:
    assert count_tokens(STATIC_CORE) <= BUDGETS["A"]


def test_static_core_has_no_obvious_dynamic_placeholders() -> None:
    forbidden = ("{{", "}}", "current time:", "timezone:", "user name", "[date]")
    assert not any(value in STATIC_CORE.lower() for value in forbidden)


def test_prompt_version_is_present() -> None:
    assert PROMPT_VERSION == "v1"
