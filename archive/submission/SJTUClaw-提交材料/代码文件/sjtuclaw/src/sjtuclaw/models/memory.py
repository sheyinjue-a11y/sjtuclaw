"""Cross-session long-term memory models."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class MemoryCategory(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    PROJECT = "project"
    CONSTRAINT = "constraint"
    EVENT = "event"
    OTHER = "other"


class MemorySourceKind(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"


def _require_utc(timestamp: datetime, field_name: str) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC")


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    memory_id: str
    content: str
    created_at: datetime
    category: MemoryCategory = MemoryCategory.OTHER
    importance: int = 3
    source_kind: MemorySourceKind = MemorySourceKind.MANUAL
    source_session_id: str | None = None
    source_message_id: str | None = None
    original_time_text: str | None = None
    event_at: datetime | None = None
    event_timezone: str | None = None
    updated_at: datetime | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not self.memory_id:
            raise ValueError("memory_id must not be empty")
        if not self.content.strip():
            raise ValueError("memory content must not be empty")
        if not isinstance(self.category, MemoryCategory):
            raise ValueError("category must be a MemoryCategory")
        if type(self.importance) is not int or not 1 <= self.importance <= 5:
            raise ValueError("importance must be an integer from 1 to 5")
        if not isinstance(self.source_kind, MemorySourceKind):
            raise ValueError("source_kind must be a MemorySourceKind")
        _require_utc(self.created_at, "created_at")
        updated_at = self.created_at if self.updated_at is None else self.updated_at
        _require_utc(updated_at, "updated_at")
        if updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        object.__setattr__(self, "updated_at", updated_at)
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if self.source_kind is MemorySourceKind.AUTOMATIC and (
            not self.source_session_id or not self.source_message_id
        ):
            raise ValueError("automatic memory requires provenance")
        if (self.event_at is None) != (self.event_timezone is None):
            raise ValueError("event_at and event_timezone must be provided together")
        if self.event_at is not None:
            _require_utc(self.event_at, "event_at")
        if self.event_timezone is not None and not self.event_timezone.strip():
            raise ValueError("event_timezone must not be empty")


@dataclass(frozen=True, slots=True)
class Memory:
    entries: tuple[MemoryEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class CreateMemory:
    content: str
    category: MemoryCategory = MemoryCategory.OTHER
    importance: int = 3
    original_time_text: str | None = None
    event_at: datetime | None = None
    event_timezone: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateMemory:
    content: str
    category: MemoryCategory
    importance: int
    original_time_text: str | None = None
    event_at: datetime | None = None
    event_timezone: str | None = None
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class MemoryPage:
    entries: tuple[MemoryEntry, ...]
    next_cursor: str | None
