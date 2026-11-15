from datetime import UTC, datetime, timedelta

import pytest

from sjtuclaw.models.memory import MemoryCategory, MemoryEntry, MemorySourceKind

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def test_automatic_memory_requires_provenance() -> None:
    with pytest.raises(ValueError, match="provenance"):
        MemoryEntry(
            memory_id="memory_1",
            content="用户偏好中文回答",
            category=MemoryCategory.PREFERENCE,
            importance=5,
            source_kind=MemorySourceKind.AUTOMATIC,
            source_session_id=None,
            source_message_id=None,
            created_at=NOW,
            updated_at=NOW,
        )


def test_memory_entry_rejects_empty_content() -> None:
    with pytest.raises(ValueError, match="memory content"):
        MemoryEntry("memory_1", "  ", NOW)


@pytest.mark.parametrize("importance", [0, 6, True])
def test_memory_entry_rejects_invalid_importance(importance: int) -> None:
    with pytest.raises(ValueError, match="importance"):
        MemoryEntry("memory_1", "内容", NOW, importance=importance)


def test_memory_entry_rejects_non_utc_and_reverse_timestamps() -> None:
    with pytest.raises(ValueError, match="UTC"):
        MemoryEntry("memory_1", "内容", datetime(2026, 7, 15, 16, 0))
    with pytest.raises(ValueError, match="updated_at"):
        MemoryEntry("memory_1", "内容", NOW, updated_at=NOW - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("event_at", "event_timezone"),
    [(NOW, None), (None, "Asia/Shanghai")],
)
def test_memory_entry_requires_event_time_and_timezone_together(
    event_at: datetime | None,
    event_timezone: str | None,
) -> None:
    with pytest.raises(ValueError, match="event_at and event_timezone"):
        MemoryEntry(
            "memory_1",
            "内容",
            NOW,
            event_at=event_at,
            event_timezone=event_timezone,
        )
