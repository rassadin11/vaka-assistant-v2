"""Tests for registered prompt variants and versioned eval scenarios."""

from __future__ import annotations

import pytest

from core.context_manager import UserDynamics, build_context
from core.prompt import PROMPTS, STATIC_CORE, get_prompt, prompt_version_for_model
from evals.scenarios_v1 import build_scenarios

_EXPECTED_V1_IDS = [
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


def test_prompt_registry_returns_v1_and_rejects_unknown_versions() -> None:
    assert get_prompt("v1") is STATIC_CORE
    with pytest.raises(ValueError, match="Known versions: v1, v2-flash"):
        get_prompt("unknown")


def test_prompt_version_for_model_uses_mapped_or_default_version() -> None:
    assert prompt_version_for_model("deepseek/deepseek-v4-flash") == "v2-flash"
    assert prompt_version_for_model("deepseek/deepseek-chat") == "v1"


def test_versioned_scenario_builds_only_change_system_message() -> None:
    v1_scenarios = build_scenarios("v1")
    flash_scenarios = build_scenarios("v2-flash")

    assert len(v1_scenarios) == len(flash_scenarios) == 39
    assert [scenario.id for scenario in v1_scenarios[:20]] == _EXPECTED_V1_IDS
    assert [scenario.id for scenario in flash_scenarios[:20]] == _EXPECTED_V1_IDS

    for v1_scenario, flash_scenario in zip(v1_scenarios, flash_scenarios, strict=True):
        assert v1_scenario.id == flash_scenario.id
        assert v1_scenario.description == flash_scenario.description
        assert v1_scenario.tools == flash_scenario.tools
        assert v1_scenario.messages[1:] == flash_scenario.messages[1:]
        assert v1_scenario.messages[0].role == flash_scenario.messages[0].role == "system"
        assert v1_scenario.messages[0].content != flash_scenario.messages[0].content
