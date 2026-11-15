"""Session management use cases shared by entrypoints."""

from dataclasses import replace

from sjtuclaw.models.session import Session, SessionArchiveReason
from sjtuclaw.ports.runtime import Clock, IdGenerator
from sjtuclaw.ports.stores import SessionStore
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_state import require_session_mutable


class SessionService:
    def __init__(
        self,
        *,
        sessions: SessionStore,
        coordinator: SessionCoordinator,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._sessions = sessions
        self._coordinator = coordinator
        self._clock = clock
        self._ids = ids

    def create(self, title: str = "新会话") -> Session:
        now = self._clock.now()
        session = Session(
            session_id=self._ids.new_id("session"),
            title=title,
            messages=(),
            summary="",
            created_at=now,
            updated_at=now,
            token_tracking_started_at=now,
        )
        self._sessions.create(session)
        return session

    def list(self, *, archived: bool = False) -> tuple[Session, ...]:
        return self._sessions.list(archived=archived)

    def load(self, session_id: str) -> Session:
        return self._sessions.load(session_id)

    async def rename(self, session_id: str, title: str) -> Session:
        async with self._coordinator.execution_lock(session_id):
            async with self._coordinator.admission_lock(session_id):
                session = self._sessions.load(session_id)
                require_session_mutable(session)
                renamed = replace(session, title=title, updated_at=self._clock.now())
                self._sessions.save(renamed)
                return renamed

    async def delete(self, session_id: str) -> None:
        async with self._coordinator.execution_lock(session_id):
            self._sessions.delete(session_id)

    async def archive(self, session_id: str) -> Session:
        async with self._coordinator.execution_lock(session_id):
            session = self._sessions.load(session_id)
            if session.archived_at is not None:
                return session
            archived = replace(
                session,
                archived_at=self._clock.now(),
                archived_reason=SessionArchiveReason.USER_ARCHIVED,
                updated_at=self._clock.now(),
            )
            self._sessions.save(archived)
            return archived

    async def restore(self, session_id: str) -> Session:
        async with self._coordinator.execution_lock(session_id):
            session = self._sessions.load(session_id)
            if session.archived_at is None:
                return session
            restored = replace(
                session,
                archived_at=None,
                archived_reason=None,
                updated_at=self._clock.now(),
            )
            self._sessions.save(restored)
            return restored
