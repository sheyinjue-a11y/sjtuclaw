"""Synchronous CRUD and query use cases for long-term memory."""

import base64
import binascii
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sjtuclaw.errors import StoreError
from sjtuclaw.models.memory import (
    CreateMemory,
    Memory,
    MemoryCategory,
    MemoryEntry,
    MemoryPage,
    MemorySourceKind,
    UpdateMemory,
)
from sjtuclaw.ports.stores import MemoryStore
from sjtuclaw.runtime.memory_coordinator import MemoryCoordinator
from sjtuclaw.runtime.memory_extractor import (
    MAX_MEMORY_OPERATIONS,
    MAX_OPERATION_CONTENT_LENGTH,
    MemoryOperation,
    MemoryOperationType,
)
from sjtuclaw.runtime.memory_safety import ensure_safe_memory_content


class MemoryRevisionConflict(Exception):
    """The caller tried to change an entry using an older revision."""


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        coordinator: MemoryCoordinator,
        *,
        clock: Callable[[], datetime] | None = None,
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._clock = clock or (lambda: datetime.now(UTC))
        self._new_id = new_id or (lambda: f"memory_{uuid4().hex}")

    def list_page(self, *, query: str, cursor: str | None, limit: int) -> MemoryPage:
        if limit < 1:
            raise ValueError("limit must be positive")
        normalized_query = query.strip().casefold()
        with self._coordinator.locked():
            entries = tuple(
                entry
                for entry in self._ordered(self._store.load())
                if self._matches(entry, normalized_query)
            )
        start = 0 if cursor is None else self._cursor_start(entries, cursor)
        page_entries = entries[start : start + limit]
        next_cursor = (
            None
            if start + len(page_entries) >= len(entries)
            else self._encode_cursor(page_entries[-1])
        )
        return MemoryPage(page_entries, next_cursor)

    def create_manual(self, command: CreateMemory) -> MemoryEntry:
        ensure_safe_memory_content(command.content)
        now = self._clock()
        entry = MemoryEntry(
            memory_id=self._new_id(),
            content=command.content,
            category=command.category,
            importance=command.importance,
            original_time_text=command.original_time_text,
            event_at=command.event_at,
            event_timezone=command.event_timezone,
            created_at=now,
            updated_at=now,
        )
        with self._coordinator.locked():
            memory = self._store.load()
            self._store.replace(Memory((*memory.entries, entry)))
        return entry

    def update_manual(self, memory_id: str, command: UpdateMemory) -> MemoryEntry:
        ensure_safe_memory_content(command.content)
        with self._coordinator.locked():
            memory = self._store.load()
            current = self._find(memory, memory_id)
            self._require_current_revision(current, command.revision)
            updated = replace(
                current,
                content=command.content,
                category=command.category,
                importance=command.importance,
                original_time_text=command.original_time_text,
                event_at=command.event_at,
                event_timezone=command.event_timezone,
                updated_at=self._clock(),
                revision=current.revision + 1,
            )
            self._store.replace(
                Memory(tuple(updated if entry.memory_id == memory_id else entry for entry in memory.entries))
            )
        return updated

    def delete_manual(self, memory_id: str, *, revision: int | None) -> None:
        with self._coordinator.locked():
            memory = self._store.load()
            current = self._find(memory, memory_id)
            self._require_current_revision(current, revision)
            self._store.replace(Memory(tuple(entry for entry in memory.entries if entry != current)))

    def apply_automatic_batch(
        self,
        operations: tuple[MemoryOperation, ...],
        *,
        source_session_id: str,
        source_message_id: str,
    ) -> tuple[MemoryEntry, ...]:
        if (
            type(operations) is not tuple
            or len(operations) > MAX_MEMORY_OPERATIONS
            or type(source_session_id) is not str
            or not source_session_id.strip()
            or type(source_message_id) is not str
            or not source_message_id.strip()
        ):
            raise ValueError("automatic memory batch is invalid")

        with self._coordinator.locked():
            memory = self._store.load()
            by_id = {entry.memory_id: entry for entry in memory.entries}
            if len(by_id) != len(memory.entries):
                raise ValueError("automatic memory batch is invalid")
            touched_ids: set[str] = set()
            for operation in operations:
                self._validate_automatic_operation(operation)
                if operation.operation_type is MemoryOperationType.UPDATE:
                    assert operation.memory_id is not None
                    if operation.memory_id in touched_ids:
                        raise ValueError("duplicate automatic memory update target")
                    touched_ids.add(operation.memory_id)
            for operation in operations:
                if operation.operation_type is MemoryOperationType.UPDATE:
                    assert operation.memory_id is not None
                    current = by_id.get(operation.memory_id)
                    if current is None or current.revision != operation.revision:
                        return ()

            now = self._clock()
            if (
                not isinstance(now, datetime)
                or now.tzinfo is None
                or now.utcoffset() is None
                or now.utcoffset() != timedelta(0)
            ):
                raise ValueError("automatic memory clock must return UTC")
            changed: list[MemoryEntry] = []
            replacements: dict[str, MemoryEntry] = {}
            additions: list[MemoryEntry] = []
            generated_ids = set(by_id)
            for operation in operations:
                if operation.operation_type is MemoryOperationType.NOOP:
                    continue
                assert operation.content is not None
                assert operation.category is not None
                assert operation.importance is not None
                if operation.operation_type is MemoryOperationType.ADD:
                    memory_id = self._new_id()
                    if memory_id in generated_ids:
                        raise ValueError("automatic memory ID collision")
                    generated_ids.add(memory_id)
                    entry = MemoryEntry(
                        memory_id=memory_id,
                        content=operation.content,
                        category=operation.category,
                        importance=operation.importance,
                        source_kind=MemorySourceKind.AUTOMATIC,
                        source_session_id=source_session_id,
                        source_message_id=source_message_id,
                        original_time_text=operation.original_time_text,
                        event_at=operation.event_at,
                        event_timezone=operation.event_timezone,
                        created_at=now,
                        updated_at=now,
                    )
                    additions.append(entry)
                else:
                    assert operation.memory_id is not None
                    current = by_id[operation.memory_id]
                    entry = replace(
                        current,
                        content=operation.content,
                        category=operation.category,
                        importance=operation.importance,
                        source_kind=MemorySourceKind.AUTOMATIC,
                        source_session_id=source_session_id,
                        source_message_id=source_message_id,
                        original_time_text=operation.original_time_text,
                        event_at=operation.event_at,
                        event_timezone=operation.event_timezone,
                        updated_at=now,
                        revision=current.revision + 1,
                    )
                    replacements[entry.memory_id] = entry
                changed.append(entry)

            if changed:
                entries = tuple(
                    replacements.get(entry.memory_id, entry) for entry in memory.entries
                )
                self._store.replace(Memory((*entries, *additions)))
            return tuple(changed)

    @staticmethod
    def _validate_automatic_operation(operation: MemoryOperation) -> None:
        if not isinstance(operation, MemoryOperation):
            raise ValueError("automatic memory operation is invalid")
        if type(operation.operation_type) is not MemoryOperationType:
            raise ValueError("automatic memory operation type is invalid")
        if operation.operation_type is MemoryOperationType.NOOP:
            if any(
                value is not None
                for value in (
                    operation.content,
                    operation.category,
                    operation.importance,
                    operation.memory_id,
                    operation.revision,
                    operation.original_time_text,
                    operation.event_at,
                    operation.event_timezone,
                )
            ):
                raise ValueError("automatic NOOP fields are invalid")
            return
        if operation.operation_type not in {
            MemoryOperationType.ADD,
            MemoryOperationType.UPDATE,
        }:
            raise ValueError("automatic memory operation type is invalid")
        if (
            type(operation.content) is not str
            or not operation.content.strip()
            or len(operation.content) > MAX_OPERATION_CONTENT_LENGTH
            or not isinstance(operation.category, MemoryCategory)
            or type(operation.importance) is not int
            or not 1 <= operation.importance <= 5
            or (operation.event_at is None) != (operation.event_timezone is None)
            or (
                operation.original_time_text is not None
                and (
                    type(operation.original_time_text) is not str
                    or not operation.original_time_text.strip()
                )
            )
        ):
            raise ValueError("automatic memory operation is invalid")
        if operation.event_at is not None:
            if (
                not isinstance(operation.event_at, datetime)
                or operation.event_at.tzinfo is None
                or operation.event_at.utcoffset() is None
                or operation.event_at.utcoffset() != timedelta(0)
                or type(operation.event_timezone) is not str
                or not operation.event_timezone.strip()
            ):
                raise ValueError("automatic memory operation is invalid")
            try:
                ZoneInfo(operation.event_timezone)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("automatic memory operation is invalid") from None
        ensure_safe_memory_content(operation.content)
        if operation.operation_type is MemoryOperationType.ADD:
            if operation.memory_id is not None or operation.revision is not None:
                raise ValueError("automatic ADD identity fields are invalid")
        elif (
            type(operation.memory_id) is not str
            or not operation.memory_id
            or type(operation.revision) is not int
            or operation.revision < 1
        ):
            raise ValueError("automatic UPDATE identity fields are invalid")

    @staticmethod
    def _ordered(memory: Memory) -> tuple[MemoryEntry, ...]:
        by_memory_id = sorted(memory.entries, key=lambda entry: entry.memory_id)
        return tuple(sorted(by_memory_id, key=MemoryService._memory_moment, reverse=True))

    @staticmethod
    def _memory_moment(entry: MemoryEntry) -> datetime:
        return entry.event_at or entry.created_at

    @staticmethod
    def _matches(entry: MemoryEntry, query: str) -> bool:
        if not query:
            return True
        fields = (entry.content, entry.category.value, entry.original_time_text or "")
        return any(query in field.casefold() for field in fields)

    @staticmethod
    def _find(memory: Memory, memory_id: str) -> MemoryEntry:
        for entry in memory.entries:
            if entry.memory_id == memory_id:
                return entry
        raise StoreError(f"memory not found: {memory_id}")

    @staticmethod
    def _require_current_revision(entry: MemoryEntry, revision: int | None) -> None:
        if revision is not None and revision != entry.revision:
            raise MemoryRevisionConflict("memory revision conflict")

    @staticmethod
    def _encode_cursor(entry: MemoryEntry) -> str:
        payload = f"{MemoryService._memory_moment(entry).isoformat()}|{entry.memory_id}".encode()
        return base64.urlsafe_b64encode(payload).decode("ascii")

    @staticmethod
    def _cursor_start(entries: tuple[MemoryEntry, ...], cursor: str) -> int:
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
            moment_text, memory_id = decoded.rsplit("|", 1)
            moment = datetime.fromisoformat(moment_text)
            if moment.tzinfo is None or moment.utcoffset() != timedelta(0) or not memory_id:
                raise ValueError
        except (binascii.Error, UnicodeError, ValueError) as exc:
            raise ValueError("invalid cursor") from exc
        for index, entry in enumerate(entries):
            entry_moment = MemoryService._memory_moment(entry)
            if entry_moment < moment or (
                entry_moment == moment and entry.memory_id > memory_id
            ):
                return index
        return len(entries)
