"""Storage contract for session-scoped uploaded attachments."""

from collections.abc import AsyncIterable
from datetime import datetime
from typing import BinaryIO, Protocol

from sjtuclaw.models.attachment import Attachment, AttachmentRead


class AttachmentStore(Protocol):
    async def ingest_text(
        self,
        *,
        session_id: str,
        attachment_id: str,
        display_name: str,
        chunks: AsyncIterable[bytes],
        created_at: datetime,
    ) -> Attachment: ...

    async def ingest_pdf(
        self,
        *,
        session_id: str,
        attachment_id: str,
        display_name: str,
        chunks: AsyncIterable[bytes],
        created_at: datetime,
    ) -> Attachment: ...

    def list(self, session_id: str) -> tuple[Attachment, ...]: ...

    def load(self, session_id: str, attachment_id: str) -> Attachment: ...

    def require_ready(
        self,
        session_id: str,
        attachment_refs: tuple[str, ...],
    ) -> tuple[Attachment, ...]: ...

    def read_normalized(self, session_id: str, attachment_id: str) -> str: ...

    def open_original(self, session_id: str, attachment_id: str) -> BinaryIO: ...

    def read_slice(
        self,
        session_id: str,
        attachment_id: str,
        *,
        cursor: str | None,
        max_chars: int,
    ) -> AttachmentRead: ...

    def delete(self, session_id: str, attachment_id: str) -> None: ...
