"""Pure calendar occurrence expansion and cron descriptions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from core.calendar_view import CalendarTask, expand_occurrences, repeat_human


def _task(
    task_id: int,
    next_run_at: datetime,
    *,
    cron: str | None = None,
    kind: str = "reminder",
    status: str = "active",
) -> CalendarTask:
    return CalendarTask(
        id=task_id,
        kind=kind,
        title=f"Task {task_id}",
        payload=f"Reminder {task_id}",
        cron_expr=cron,
        next_run_at=next_run_at,
        status=status,
    )


def test_one_off_range_is_inclusive_by_local_date_and_end_exclusive() -> None:
    timezone = ZoneInfo("Europe/Moscow")
    tasks = [
        _task(1, datetime(2026, 7, 1, 0, tzinfo=timezone)),
        _task(2, datetime(2026, 7, 31, 23, 59, tzinfo=timezone), status="done"),
        _task(3, datetime(2026, 8, 1, 0, tzinfo=timezone)),
    ]

    occurrences = expand_occurrences(tasks, date(2026, 7, 1), date(2026, 7, 31), timezone)

    assert [item.id for item in occurrences] == [1, 2]
    assert occurrences[0].occurs_at == "2026-07-01T00:00:00+03:00"
    assert occurrences[1].status == "done"


def test_daily_cron_includes_exact_start_boundary() -> None:
    timezone = ZoneInfo("UTC")
    occurrences = expand_occurrences(
        [_task(1, datetime(2026, 7, 1, tzinfo=UTC), cron="0 0 * * *")],
        date(2026, 7, 1),
        date(2026, 7, 3),
        timezone,
    )

    assert [item.occurs_at for item in occurrences] == [
        "2026-07-01T00:00:00+00:00",
        "2026-07-02T00:00:00+00:00",
        "2026-07-03T00:00:00+00:00",
    ]


def test_weekly_cron_expands_monday_in_user_timezone() -> None:
    timezone = ZoneInfo("Europe/Moscow")
    occurrences = expand_occurrences(
        [_task(1, datetime(2026, 7, 1, tzinfo=UTC), cron="30 18 * * 1")],
        date(2026, 7, 1),
        date(2026, 7, 14),
        timezone,
    )

    assert [item.local_date for item in occurrences] == ["2026-07-06", "2026-07-13"]
    assert all(item.time_local == "18:30" for item in occurrences)


def test_monthly_cron_only_emits_dates_inside_range() -> None:
    timezone = ZoneInfo("Asia/Tokyo")
    occurrences = expand_occurrences(
        [_task(1, datetime(2026, 6, 1, tzinfo=UTC), cron="0 12 1 * *")],
        date(2026, 6, 15),
        date(2026, 8, 2),
        timezone,
    )

    assert [item.local_date for item in occurrences] == ["2026-07-01", "2026-08-01"]


def test_one_off_uses_current_timezone_not_original_offset() -> None:
    timezone = ZoneInfo("America/New_York")
    occurrence = expand_occurrences(
        [_task(1, datetime(2026, 7, 2, 1, tzinfo=UTC))],
        date(2026, 7, 1),
        date(2026, 7, 1),
        timezone,
    )[0]

    assert occurrence.local_date == "2026-07-01"
    assert occurrence.time_local == "21:00"
    assert occurrence.occurs_at.endswith("-04:00")


def test_dst_transition_keeps_cron_occurrences_in_current_timezone() -> None:
    timezone = ZoneInfo("Europe/Berlin")
    occurrences = expand_occurrences(
        [_task(1, datetime(2026, 3, 27, tzinfo=UTC), cron="30 2 * * *")],
        date(2026, 3, 28),
        date(2026, 3, 30),
        timezone,
    )

    assert [item.local_date for item in occurrences] == [
        "2026-03-28",
        "2026-03-29",
        "2026-03-30",
    ]
    assert all(item.occurs_at[-6:] in {"+01:00", "+02:00"} for item in occurrences)


def test_frequent_cron_is_capped_and_marked_truncated() -> None:
    occurrences = expand_occurrences(
        [_task(1, datetime(2026, 7, 1, tzinfo=UTC), cron="* * * * *")],
        date(2026, 7, 1),
        date(2026, 7, 1),
        ZoneInfo("UTC"),
    )

    assert len(occurrences) == 100
    assert all(item.truncated for item in occurrences)


def test_cancelled_tasks_are_never_expanded() -> None:
    occurrences = expand_occurrences(
        [_task(1, datetime(2026, 7, 1, tzinfo=UTC), status="cancelled")],
        date(2026, 7, 1),
        date(2026, 7, 1),
        ZoneInfo("UTC"),
    )

    assert occurrences == []


def test_repeat_human_covers_supported_patterns_and_fallbacks() -> None:
    assert repeat_human("0 9 * * *") == "каждый день в 09:00"
    assert repeat_human("30 18 * * 1") == "по понедельникам в 18:30"
    assert repeat_human("0 12 1 * *") == "1-го числа в 12:00"  # noqa: RUF001
    assert repeat_human("*/5 * * * *") == "по расписанию"
    assert repeat_human("broken") == "по расписанию"
