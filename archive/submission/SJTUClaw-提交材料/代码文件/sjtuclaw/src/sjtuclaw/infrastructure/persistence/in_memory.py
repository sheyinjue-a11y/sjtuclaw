"""In-memory Store used by Step 1 and deterministic runtime tests."""

from sjtuclaw.errors import StoreError
from sjtuclaw.models.session import Session


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self, session: Session) -> None:
        if session.session_id in self._sessions:
            raise StoreError(f"session already exists: {session.session_id}")
        self._sessions[session.session_id] = session

    def list(self, *, archived: bool = False) -> tuple[Session, ...]:
        values = (
            session
            for session in self._sessions.values()
            if (session.archived_at is not None) == archived
        )
        by_id = sorted(values, key=lambda session: session.session_id)
        return tuple(sorted(by_id, key=lambda session: session.updated_at, reverse=True))

    def load(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise StoreError(f"session not found: {session_id}") from exc

    def save(self, session: Session) -> None:
        if session.session_id not in self._sessions:
            raise StoreError(f"session not found: {session.session_id}")
        self._sessions[session.session_id] = session

    def delete(self, session_id: str) -> None:
        try:
            del self._sessions[session_id]
        except KeyError as exc:
            raise StoreError(f"session not found: {session_id}") from exc
