"""Session-reference checks around AttachmentStore mutations."""

from collections.abc import AsyncIterable
from datetime import datetime

from sjtuclaw.errors import AttachmentError
from sjtuclaw.models.attachment import Attachment
from sjtuclaw.models.message import MessageRole
from sjtuclaw.ports.attachments import AttachmentStore
from sjtuclaw.ports.stores import SessionStore
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_state import require_session_mutable


class AttachmentService:
    def __init__(
        self,
        *,
        sessions: SessionStore,
        attachments: AttachmentStore,
        coordinator: SessionCoordinator,
    ) -> None:
        self._sessions = sessions
        self._attachments = attachments
        self._coordinator = coordinator

    def require_ready(
        self,
        session_id: str,
        attachment_refs: tuple[str, ...],
    ) -> None:
        self._attachments.require_ready(session_id, attachment_refs)

    def list(self, session_id: str) -> tuple[Attachment, ...]:
        """Expose session-scoped metadata without leaking AttachmentStore to entrypoints."""

        return self._attachments.list(session_id)

    async def ingest_text(
        self,
        *,
        session_id: str,
        attachment_id: str,
        display_name: str,
        chunks: AsyncIterable[bytes],
        created_at: datetime,
    ) -> Attachment:
        async with self._coordinator.admission_lock(session_id):
            require_session_mutable(self._sessions.load(session_id))
            return await self._attachments.ingest_text(
                session_id=session_id,
                attachment_id=attachment_id,
                display_name=display_name,
                chunks=chunks,
                created_at=created_at,
            )

    async def ingest_pdf(
        self,
        *,
        session_id: str,
        attachment_id: str,
        display_name: str,
        chunks: AsyncIterable[bytes],
        created_at: datetime,
    ) -> Attachment:
        async with self._coordinator.admission_lock(session_id):
            require_session_mutable(self._sessions.load(session_id))
            return await self._attachments.ingest_pdf(
                session_id=session_id,
                attachment_id=attachment_id,
                display_name=display_name,
                chunks=chunks,
                created_at=created_at,
            )

    async def delete(self, session_id: str, attachment_id: str) -> None:
        async with self._coordinator.admission_lock(session_id):
            session = self._sessions.load(session_id)
            require_session_mutable(session)
            self._attachments.load(session_id, attachment_id)
            pending_reference = any(
                attachment_id in request.attachment_refs
                for request in session.pending_requests
            )
            historical_reference = any(
                message.role is MessageRole.USER
                and attachment_id in message.attachment_refs
                for message in session.messages
            )
            if pending_reference or historical_reference:
                raise AttachmentError("ATTACHMENT_IN_USE")
            self._attachments.delete(session_id, attachment_id)
