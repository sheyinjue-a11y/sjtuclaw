"""Attachment metadata kept separately from Session JSON."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AttachmentKind(StrEnum):
    TEXT = "text"
    PDF = "pdf"


class AttachmentStatus(StrEnum):
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AttachmentRead:
    """One bounded normalized-text segment with an opaque continuation cursor."""

    content: str
    next_cursor: str | None

    def __post_init__(self) -> None:
        if self.next_cursor is not None and not self.next_cursor:
            raise ValueError("next_cursor must be non-empty when supplied")


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Attachment:
    attachment_id: str
    session_id: str
    display_name: str
    kind: AttachmentKind
    status: AttachmentStatus
    created_at: datetime
    updated_at: datetime
    size_bytes: int | None = None
    sha256: str | None = None
    normalized_char_count: int | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.attachment_id or not self.session_id:
            raise ValueError("attachment identifiers must not be empty")
        if not self.display_name or len(self.display_name) > 255:
            raise ValueError("attachment display_name must be between 1 and 255 characters")
        if any(character in self.display_name for character in ("/", "\\", "\0")):
            raise ValueError("attachment display_name must not contain path separators")
        for timestamp in (self.created_at, self.updated_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("attachment timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("attachment updated_at must not precede created_at")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("attachment size_bytes must not be negative")
        if self.normalized_char_count is not None and self.normalized_char_count < 0:
            raise ValueError("attachment normalized_char_count must not be negative")
        if self.sha256 is not None and _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("attachment sha256 must be a lowercase SHA-256 digest")
        if self.status is AttachmentStatus.READY:
            if (
                self.size_bytes is None
                or self.sha256 is None
                or self.normalized_char_count is None
                or self.error_code is not None
            ):
                raise ValueError("ready attachment requires complete metadata")
        elif self.error_code is not None and self.status is not AttachmentStatus.FAILED:
            raise ValueError("only failed attachments may have an error_code")
        elif self.status is AttachmentStatus.FAILED and not self.error_code:
            raise ValueError("failed attachment requires an error_code")
