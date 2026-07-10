"""Deterministic token counting used for context budgets."""

from __future__ import annotations

import tiktoken

_ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Return the number of ``cl100k_base`` tokens in *text*."""

    return len(_ENCODING.encode(text))


def truncate_to_tokens(text: str, limit: int) -> str:
    """Return a prefix of *text* that contains at most *limit* tokens."""

    if limit < 0:
        raise ValueError("Token limit must not be negative.")
    return _ENCODING.decode(_ENCODING.encode(text)[:limit])
