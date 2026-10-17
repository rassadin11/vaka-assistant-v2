"""Offline coverage for the pure history-rendering helpers of the dialogue store."""

from __future__ import annotations

from uuid import UUID

from core.dialog_store import StoredMessage, to_llm_messages


def _stored(content: str, meta: dict[str, object]) -> StoredMessage:
    return StoredMessage(
        id=UUID("018f0000-0000-7000-8000-000000000010"),
        role="user",
        content=content,
        tool_calls=None,
        tool_call_id=None,
        tokens=2,
        meta=meta,
    )


def test_scheduled_task_instruction_is_marked_in_the_rendered_tail() -> None:
    rendered = to_llm_messages(
        [
            _stored("собери сводку расходов", {"source": "agent_task", "scheduled_task_id": 7}),
            _stored("сколько я потратил?", {}),
            _stored("голосом", {"modality": "voice", "duration": 3}),
        ]
    )

    assert rendered[0].content == "[scheduled task] собери сводку расходов"
    assert rendered[1].content == "сколько я потратил?"
    assert rendered[2].content == "голосом"
