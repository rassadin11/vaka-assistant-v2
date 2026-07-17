"""Optional Mini App entry buttons attached to worker replies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MiniAppScreen = Literal["calendar", "finance"]

FINANCE_TOOLS: frozenset[str] = frozenset(
    {"add_transaction", "query_transactions", "set_budget", "get_budget_status"}
)
CALENDAR_TOOLS: frozenset[str] = frozenset({"create_reminder", "list_reminders", "cancel_reminder"})


@dataclass(frozen=True, slots=True)
class MiniAppButton:
    text: str
    screen: MiniAppScreen


@dataclass(frozen=True, slots=True)
class WorkerReply:
    text: str
    mini_app_button: MiniAppButton | None = None


def mini_app_button_for_tools(tool_names: tuple[str, ...]) -> MiniAppButton | None:
    """Pick one Mini App button from invoked tools; the last relevant tool wins."""

    for name in reversed(tool_names):
        if name in FINANCE_TOOLS:
            return MiniAppButton("Открыть финансы", "finance")
        if name in CALENDAR_TOOLS:
            return MiniAppButton("Открыть календарь", "calendar")
    return None
