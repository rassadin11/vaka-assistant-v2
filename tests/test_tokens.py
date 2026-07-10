"""Tests for deterministic context token counting."""

from __future__ import annotations

from core.tokens import count_tokens


def test_count_tokens_is_non_zero_for_text() -> None:
    assert count_tokens("Hello, мир!") > 0


def test_count_tokens_is_monotonic_for_longer_text() -> None:
    assert count_tokens("short text") < count_tokens("short text with several additional words")
