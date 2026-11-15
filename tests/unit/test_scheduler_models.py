"""Deterministic Scheduler schedule-rule contracts."""

from datetime import UTC, datetime

import pytest

from sjtuclaw.models.scheduler import (
    SCHEDULER_MIN_INTERVAL_SECONDS,
    Schedule,
    ScheduleKind,
    next_run_after,
)


def test_interval_next_run_uses_anchor_without_drift() -> None:
    anchor = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    schedule = Schedule(
        ScheduleKind.INTERVAL,
        anchor,
        interval_seconds=SCHEDULER_MIN_INTERVAL_SECONDS,
    )

    assert next_run_after(schedule, datetime(2026, 7, 20, 0, 12, tzinfo=UTC)) == datetime(
        2026, 7, 20, 0, 15, tzinfo=UTC
    )


def test_daily_and_weekly_schedules_preserve_local_calendar_rule() -> None:
    anchor = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    daily = Schedule(ScheduleKind.DAILY, anchor, timezone="Asia/Shanghai", hour=9, minute=30)
    weekly = Schedule(
        ScheduleKind.WEEKLY,
        anchor,
        timezone="Asia/Shanghai",
        hour=9,
        minute=30,
        weekdays=(0, 2),
    )
    reference = datetime(2026, 7, 20, 2, 0, tzinfo=UTC)  # Monday, 10:00 Shanghai

    assert next_run_after(daily, reference) == datetime(2026, 7, 21, 1, 30, tzinfo=UTC)
    assert next_run_after(weekly, reference) == datetime(2026, 7, 22, 1, 30, tzinfo=UTC)


def test_invalid_scheduler_rules_are_rejected() -> None:
    anchor = datetime(2026, 7, 20, tzinfo=UTC)

    with pytest.raises(ValueError, match="minimum interval"):
        Schedule(ScheduleKind.INTERVAL, anchor, interval_seconds=299)
    with pytest.raises(ValueError, match="timezone"):
        Schedule(ScheduleKind.DAILY, anchor, timezone="bad/timezone", hour=9, minute=0)
    with pytest.raises(ValueError, match="weekdays"):
        Schedule(ScheduleKind.WEEKLY, anchor, timezone="Asia/Shanghai", hour=9, minute=0)
