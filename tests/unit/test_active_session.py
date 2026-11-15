from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.models.session import Session, SessionArchiveReason
from sjtuclaw.runtime.active_session import ActiveSessionCoordinator

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def make_session(session_id: str, title: str) -> Session:
    return Session(session_id, title, (), "", NOW, NOW)


class StubSessions:
    def __init__(self, values: list[Session]) -> None:
        self.values = values
        self.created = 0

    def list(self, *, archived: bool = False) -> tuple[Session, ...]:
        return tuple(item for item in self.values if (item.archived_at is not None) == archived)

    def load(self, session_id: str) -> Session:
        for item in self.values:
            if item.session_id == session_id:
                return item
        raise StoreError("missing")

    def create(self, title: str = "新会话") -> Session:
        self.created += 1
        item = make_session(f"session-{self.created}", title)
        self.values.insert(0, item)
        return item


def test_resolve_uses_selected_session_then_falls_back_to_most_recent() -> None:
    recent = make_session("recent", "最近")
    selected = make_session("selected", "选中")
    sessions = StubSessions([recent, selected])
    coordinator = ActiveSessionCoordinator(sessions)

    assert coordinator.select("selected").session_id == "selected"
    sessions.values = [
        recent,
        replace(
            selected,
            archived_at=NOW,
            archived_reason=SessionArchiveReason.USER_ARCHIVED,
        ),
    ]
    assert coordinator.resolve().session_id == "recent"
    sessions.values = [recent, selected]
    coordinator.select("selected")
    sessions.values = [recent]
    assert coordinator.resolve().session_id == "recent"


def test_resolve_creates_when_no_unarchived_session_exists() -> None:
    sessions = StubSessions([])
    assert ActiveSessionCoordinator(sessions).resolve().title == "新会话"
    assert sessions.created == 1


def test_select_rejects_archived_session() -> None:
    archived = replace(
        make_session("old", "旧会话"),
        archived_at=NOW,
        archived_reason=SessionArchiveReason.USER_ARCHIVED,
    )
    with pytest.raises(ValueError, match="archived"):
        ActiveSessionCoordinator(StubSessions([archived])).select("old")
