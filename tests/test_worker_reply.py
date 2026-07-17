"""Tests for optional Mini App buttons on worker replies."""

from worker.reply import MiniAppButton, mini_app_button_for_tools


def test_finance_tool_maps_to_finance_button() -> None:
    assert mini_app_button_for_tools(("query_transactions",)) == MiniAppButton(
        "Открыть финансы", "finance"
    )


def test_reminder_tool_maps_to_calendar_button() -> None:
    assert mini_app_button_for_tools(("create_reminder",)) == MiniAppButton(
        "Открыть календарь", "calendar"
    )


def test_last_relevant_tool_wins() -> None:
    assert mini_app_button_for_tools(("add_transaction", "cancel_reminder")) == MiniAppButton(
        "Открыть календарь", "calendar"
    )
    assert mini_app_button_for_tools(("list_reminders", "set_budget")) == MiniAppButton(
        "Открыть финансы", "finance"
    )


def test_irrelevant_and_empty_tools_have_no_button() -> None:
    assert mini_app_button_for_tools(("get_current_time",)) is None
    assert mini_app_button_for_tools(()) is None
