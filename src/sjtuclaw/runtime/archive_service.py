"""Idempotent Session archive, restore, and cascade-purge coordination."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Protocol

from sjtuclaw.models.session import Session, SessionArchiveReason
from sjtuclaw.ports.attachments import AttachmentStore
from sjtuclaw.ports.runtime import Clock
from sjtuclaw.ports.stores import SessionStore
from sjtuclaw.runtime.session_coordinator import SessionCoordinator


class ArchiveRuntime(Protocol):
    async def stop_session(self, session_id: str) -> None: ...


class ArchiveScheduler(Protocol):
    async def archive_for_session(self, session_id: str) -> tuple[object, ...]: ...

    async def purge_for_session(self, session_id: str) -> None: ...


class SessionDownloadPurger(Protocol):
    def purge_session(self, session_id: str) -> None: ...


class ArchiveService:
    """Own one retry-safe lifecycle transition for every Session."""

    def __init__(
        self,
        *,
        sessions: SessionStore,
        coordinator: SessionCoordinator,
        runtime: ArchiveRuntime,
        scheduler: ArchiveScheduler,
        attachments: AttachmentStore,
        downloads: SessionDownloadPurger,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._coordinator = coordinator
        self._runtime = runtime
        self._scheduler = scheduler
        self._attachments = attachments
        self._downloads = downloads
        self._clock = clock
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}

    async def archive_session(self, session_id: str) -> Session:
        async with self._lifecycle_lock(session_id):
            async with self._coordinator.admission_lock(session_id):
                session = self._sessions.load(session_id)
                if session.archived_at is None:
                    now = self._clock.now()
                    session = replace(
                        session,
                        archived_at=now,
                        archived_reason=SessionArchiveReason.USER_ARCHIVED,
                        updated_at=now,
                    )
                    self._sessions.save(session)

            await self._runtime.stop_session(session_id)
            async with self._coordinator.execution_lock(session_id):
                await self._scheduler.archive_for_session(session_id)
            return self._sessions.load(session_id)

    async def restore_session(self, session_id: str) -> Session:
        async with self._lifecycle_lock(session_id):
            session = self._sessions.load(session_id)
            if session.archived_at is None:
                return session
            # Finish any interrupted archive attempt before exposing a normal
            # Session again. Related Tasks intentionally remain archived.
            await self._runtime.stop_session(session_id)
            async with self._coordinator.execution_lock(session_id):
                await self._scheduler.archive_for_session(session_id)
                async with self._coordinator.admission_lock(session_id):
                    current = self._sessions.load(session_id)
                    now = self._clock.now()
                    restored = replace(
                        current,
                        archived_at=None,
                        archived_reason=None,
                        updated_at=now,
                    )
                    self._sessions.save(restored)
                    return restored

    async def purge_session(self, session_id: str) -> None:
        async with self._lifecycle_lock(session_id):
            session = self._find_session(session_id)
            if session is None:
                return
            if session.archived_at is None:
                raise ValueError("session is not archived")

            await self._runtime.stop_session(session_id)
            async with self._coordinator.execution_lock(session_id):
                await self._scheduler.purge_for_session(session_id)
                async with self._coordinator.admission_lock(session_id):
                    current = self._sessions.load(session_id)
                    if current.archived_at is None:
                        raise ValueError("session is not archived")
                    for attachment in self._attachments.list(session_id):
                        self._attachments.delete(session_id, attachment.attachment_id)
                    self._downloads.purge_session(session_id)
                    self._sessions.delete(session_id)

    def _find_session(self, session_id: str) -> Session | None:
        return next(
            (
                session
                for archived in (False, True)
                for session in self._sessions.list(archived=archived)
                if session.session_id == session_id
            ),
            None,
        )

    def _lifecycle_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._lifecycle_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lifecycle_locks[session_id] = lock
        return lock
