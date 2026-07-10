"""RLS-scoped persistence for dialogue messages and summaries."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg
import uuid_utils

from core.context_manager import SummaryContext
from core.db import user_transaction
from core.llm import LLMMessage, LLMToolCall, serialize_tool_calls
from core.tokens import count_tokens

StoredRole = Literal["user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """A persisted dialogue message, including its database identity."""

    id: uuid.UUID
    role: StoredRole
    content: str | None
    tool_calls: list[LLMToolCall] | None
    tool_call_id: str | None
    tokens: int | None
    meta: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DialogHistory:
    """The latest summary and the unsummarized message tail for a user."""

    summary: SummaryContext | None
    tail: list[StoredMessage]


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """A new user, assistant, or tool message awaiting persistence."""

    role: StoredRole
    content: str | None = None
    tool_calls: list[LLMToolCall] | None = None
    tool_call_id: str | None = None
    meta: dict[str, Any] | None = None


async def load_dialog(pool: asyncpg.Pool, user_id: uuid.UUID) -> DialogHistory:
    """Load the newest summary and up to 200 messages after its boundary."""

    async with user_transaction(pool, user_id) as connection:
        summary_row = await connection.fetchrow(
            """
            SELECT summary, tokens, upto_message_id
            FROM dialog_summaries
            ORDER BY created_at DESC NULLS LAST, id DESC
            LIMIT 1
            """
        )
        upto_message_id = None if summary_row is None else summary_row["upto_message_id"]
        rows = await connection.fetch(
            """
            SELECT id, role, content, tool_calls, tool_call_id, tokens, meta
            FROM messages
            WHERE ($1::uuid IS NULL OR id > $1)
            ORDER BY id
            LIMIT 200
            """,
            upto_message_id,
        )

    summary = None
    if summary_row is not None:
        summary = SummaryContext(summary_row["summary"], summary_row["tokens"])
    return DialogHistory(summary=summary, tail=[_stored_message(row) for row in rows])


def to_llm_messages(tail: Sequence[StoredMessage]) -> list[LLMMessage]:
    """Discard storage-only fields while retaining the provider-neutral chat format."""

    return [
        LLMMessage(
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
        )
        for message in tail
    ]


async def save_messages(
    pool: asyncpg.Pool,
    user_id: uuid.UUID,
    drafts: Sequence[MessageDraft],
    trace_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Insert drafts in order in a single user-scoped transaction."""

    ids: list[uuid.UUID] = []
    async with user_transaction(pool, user_id) as connection:
        for draft in drafts:
            message_id = _uuid7()
            serialized_calls = serialize_tool_calls(draft.tool_calls)
            token_text = "\n".join(
                part
                for part in (draft.content, serialized_calls)
                if part is not None and part != ""
            )
            await connection.execute(
                """
                INSERT INTO messages (
                    id, user_id, role, content, tool_calls, tool_call_id, tokens, meta, trace_id,
                    created_at
                )
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8::jsonb, $9, now())
                """,
                message_id,
                user_id,
                draft.role,
                draft.content,
                serialized_calls,
                draft.tool_call_id,
                count_tokens(token_text),
                json.dumps(draft.meta or {}, ensure_ascii=False, sort_keys=True),
                trace_id,
            )
            ids.append(message_id)
    return ids


async def save_summary(
    pool: asyncpg.Pool,
    user_id: uuid.UUID,
    text: str,
    upto_message_id: uuid.UUID,
    tokens: int,
) -> None:
    """Store a summary boundary for a user's older dialogue."""

    async with user_transaction(pool, user_id) as connection:
        await connection.execute(
            """
            INSERT INTO dialog_summaries (id, user_id, summary, upto_message_id, tokens, created_at)
            VALUES ($1, $2, $3, $4, $5, now())
            """,
            _uuid7(),
            user_id,
            text,
            upto_message_id,
            tokens,
        )


def _stored_message(row: asyncpg.Record) -> StoredMessage:
    role = row["role"]
    if role not in {"user", "assistant", "tool"}:
        raise ValueError(f"Unsupported stored message role: {role!r}")
    return StoredMessage(
        id=row["id"],
        role=role,
        content=row["content"],
        tool_calls=_parse_tool_calls(row["tool_calls"]),
        tool_call_id=row["tool_call_id"],
        tokens=row["tokens"],
        meta=_parse_object(row["meta"]),
    )


def _parse_tool_calls(raw_value: object) -> list[LLMToolCall] | None:
    if raw_value is None:
        return None
    parsed = _parse_json(raw_value)
    if not isinstance(parsed, list):
        raise ValueError("Stored tool_calls must be a JSON array.")
    return [LLMToolCall.model_validate(item) for item in parsed]


def _parse_object(raw_value: object) -> dict[str, Any]:
    if raw_value is None:
        return {}
    parsed = _parse_json(raw_value)
    if not isinstance(parsed, dict):
        raise ValueError("Stored meta must be a JSON object.")
    return parsed


def _parse_json(raw_value: object) -> object:
    return json.loads(raw_value) if isinstance(raw_value, str) else raw_value


def _uuid7() -> uuid.UUID:
    return uuid.UUID(str(uuid_utils.uuid7()))
