from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sjtuclaw.runtime.memory_time import normalize_memory_time

SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("now", "text", "expected"),
    [
        (
            datetime(2026, 7, 28, 4, 0, tzinfo=UTC),
            "今天",
            datetime(2026, 7, 27, 16, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 28, 4, 0, tzinfo=UTC),
            "明天下午3点",
            datetime(2026, 7, 29, 7, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 28, 4, 0, tzinfo=UTC),
            "后天 09:30",
            datetime(2026, 7, 30, 1, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 31, 15, 0, tzinfo=UTC),
            "明天",
            datetime(2026, 7, 31, 16, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 12, 31, 12, 0, tzinfo=UTC),
            "后天上午8点",
            datetime(2027, 1, 2, 0, 0, tzinfo=UTC),
        ),
    ],
)
def test_relative_day_text_normalizes_deterministically_in_injected_timezone(
    now: datetime,
    text: str,
    expected: datetime,
) -> None:
    result = normalize_memory_time(text, now=now, timezone=SHANGHAI)

    assert result.original_time_text == text
    assert result.event_at == expected
    assert result.event_timezone == "Asia/Shanghai"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("下周一", datetime(2026, 8, 2, 16, 0, tzinfo=UTC)),
        ("下周二上午9点", datetime(2026, 8, 4, 1, 0, tzinfo=UTC)),
        ("下周三 14:05", datetime(2026, 8, 5, 6, 5, tzinfo=UTC)),
        ("下周四", datetime(2026, 8, 5, 16, 0, tzinfo=UTC)),
        ("下周五晚上8点", datetime(2026, 8, 7, 12, 0, tzinfo=UTC)),
        ("下周六", datetime(2026, 8, 7, 16, 0, tzinfo=UTC)),
        ("下周日", datetime(2026, 8, 8, 16, 0, tzinfo=UTC)),
        ("下周天", datetime(2026, 8, 8, 16, 0, tzinfo=UTC)),
    ],
)
def test_next_week_weekdays_cover_monday_through_sunday(
    text: str,
    expected: datetime,
) -> None:
    now = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)  # Tuesday noon in Shanghai.

    result = normalize_memory_time(text, now=now, timezone=SHANGHAI)

    assert result.event_at == expected
    assert result.event_timezone == "Asia/Shanghai"


@pytest.mark.parametrize("text", ["过段时间", "以后有机会"])
def test_ambiguous_time_keeps_original_without_guessing(text: str) -> None:
    result = normalize_memory_time(
        text,
        now=datetime(2026, 7, 28, 4, 0, tzinfo=UTC),
        timezone=SHANGHAI,
    )

    assert result.original_time_text == text
    assert result.event_at is None
    assert result.event_timezone is None


def test_absolute_iso_requires_timezone_and_persists_utc_with_iana_name() -> None:
    now = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)

    aware = normalize_memory_time(
        "2027-01-01T00:30:00+08:00",
        now=now,
        timezone=SHANGHAI,
    )
    naive = normalize_memory_time(
        "2027-01-01T00:30:00",
        now=now,
        timezone=SHANGHAI,
    )

    assert aware.event_at == datetime(2026, 12, 31, 16, 30, tzinfo=UTC)
    assert aware.event_timezone == "Asia/Shanghai"
    assert naive.original_time_text == "2027-01-01T00:30:00"
    assert naive.event_at is None
    assert naive.event_timezone is None

