"""Tests for registered prompt variants and versioned eval scenarios."""

from __future__ import annotations

import pytest

from core.context_manager import UserDynamics, build_context
from core.prompt import PROMPTS, STATIC_CORE, get_prompt
from evals.scenarios_v1 import build_scenarios

_EXPECTED_IDS = [
    "tool-add-transaction",
    "tool-remember-fact",
    "tool-web-search",
    "tool-send-email",
    "tool-search-documents",
    "small-talk-greeting",
    "small-talk-capabilities",
    "small-talk-thanks",
    "pending-email",
    "pending-calendar",
    "pending-email-second",
    "unknown-calendar",
    "unknown-transactions",
    "unknown-document",
    "injection-page-command",
    "injection-page-memory",
    "relative-tomorrow",
    "relative-friday",
    "language-russian",
    "language-english",
]


def _dynamics() -> UserDynamics:
    return UserDynamics(
        current_time="2026-07-10T12:30:00+05:00",
        weekday="Friday",
        timezone="Asia/Yekaterinburg",
        plan="standard",
    )


def test_every_registered_prompt_fits_block_a_budget() -> None:
    for version in PROMPTS:
        build_context(_dynamics(), prompt_version=version)


def test_prompt_registry_returns_v2_and_rejects_unknown_versions() -> None:
    assert get_prompt("v2") is STATIC_CORE
    with pytest.raises(ValueError, match="Known versions: v2"):
        get_prompt("unknown")


def test_prompt_scenarios_use_one_version() -> None:
    scenarios = build_scenarios("v2")
    assert len(scenarios) == 50
    assert [scenario.id for scenario in scenarios[:20]] == _EXPECTED_IDS
