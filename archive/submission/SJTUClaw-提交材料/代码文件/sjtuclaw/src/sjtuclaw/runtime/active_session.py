"""Resolve the Session shared by browser and desktop entrypoints."""

from typing import Protocol

from sjtuclaw.errors import StoreError
from sjtuclaw.models.session import Session


class ActiveSessionService(Protocol):
    """Session operations needed to select an active Session."""

    def create(self, title: str = "新会话") -> Session: ...

    def list(self, *, archived: bool = False) -> tuple[Session, ...]: ...

    def load(self, session_id: str) -> Session: ...


class ActiveSessionCoordinator:
    """Track a selected active Session with safe fallback behavior."""

    def __init__(self, sessions: ActiveSessionService) -> None:
        self._sessions = sessions
        self._selected_id: str | None = None

    def select(self, session_id: str) -> Session:
        session = self._sessions.load(session_id)
        if session.archived_at is not None:
            raise ValueError("active session cannot be archived")
        self._selected_id = session.session_id
        return session

    def resolve(self) -> Session:
        if self._selected_id is not None:
            try:
                selected = self._sessions.load(self._selected_id)
            except StoreError:
                selected = None
            if selected is not None and selected.archived_at is None:
                return selected
        recent = self._sessions.list(archived=False)
        session = recent[0] if recent else self._sessions.create("新会话")
        self._selected_id = session.session_id
        return session
