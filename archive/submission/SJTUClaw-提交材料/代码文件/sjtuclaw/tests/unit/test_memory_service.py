from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from sjtuclaw.infrastructure.persistence import JsonMemoryStore
from sjtuclaw.models.memory import (
    CreateMemory,
    Memory,
    MemoryCategory,
    MemoryEntry,
    MemorySourceKind,
    UpdateMemory,
)
from sjtuclaw.runtime.memory_coordinator import MemoryCoordinator
from sjtuclaw.runtime.memory_extractor import MemoryOperation, MemoryOperationType
from sjtuclaw.runtime.memory_service import MemoryRevisionConflict, MemoryService

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    return MemoryService(
        JsonMemoryStore(tmp_path),
        MemoryCoordinator(),
        clock=lambda: NOW,
    )


@pytest.fixture
def command() -> CreateMemory:
    return CreateMemory("用户偏好中文回答", MemoryCategory.PREFERENCE, 5)


def test_create_manual_sets_manual_defaults(service: MemoryService, command: CreateMemory) -> None:
    created = service.create_manual(command)

    assert created.category is MemoryCategory.PREFERENCE
    assert created.revision == 1
    assert created.created_at == NOW
    assert created.updated_at == NOW


def test_update_rejects_stale_revision_without_changing_entry(
    service: MemoryService,
    command: CreateMemory,
) -> None:
    current = service.create_manual(command)
    service.update_manual(
        current.memory_id,
        UpdateMemory("改过的内容", MemoryCategory.PREFERENCE, 4, revision=current.revision),
    )

    with pytest.raises(MemoryRevisionConflict):
        service.update_manual(
            current.memory_id,
            UpdateMemory("冲突内容", MemoryCategory.PREFERENCE, 3, revision=current.revision),
        )

    assert service.list_page(query="", cursor=None, limit=10).entries[0].content == "改过的内容"


def test_list_page_searches_metadata_and_has_stable_pages(service: MemoryService) -> None:
    first = service.create_manual(CreateMemory("第一条", MemoryCategory.PROJECT, 3))
    second = service.create_manual(
        CreateMemory("第二条", MemoryCategory.OTHER, 3, original_time_text="下周三")
    )

    page = service.list_page(query="other", cursor=None, limit=1)
    assert page.entries == (second,)
    assert page.next_cursor is None

    all_entries = service.list_page(query="", cursor=None, limit=1)
    next_entries = service.list_page(query="", cursor=all_entries.next_cursor, limit=1)
    assert set(all_entries.entries + next_entries.entries) == {first, second}
    assert all_entries.entries != next_entries.entries
    assert service.list_page(query="", cursor=None, limit=1) == all_entries


def test_secret_content_returns_generic_error_without_echoing_secret(service: MemoryService) -> None:
    secret = "sk-super-secret-token"

    with pytest.raises(ValueError, match="memory content is invalid") as exc_info:
        service.create_manual(CreateMemory(f"API key: {secret}", MemoryCategory.OTHER, 3))

    assert secret not in str(exc_info.value)


@dataclass
class RecordingMemoryStore:
    memory: Memory = Memory()
    load_count: int = 0
    replacements: list[Memory] = field(default_factory=list)

    def load(self) -> Memory:
        self.load_count += 1
        return self.memory

    def replace(self, memory: Memory) -> Memory:
        self.replacements.append(memory)
        self.memory = memory
        return memory

    def add(self, entry: MemoryEntry) -> Memory:
        self.memory = Memory((*self.memory.entries, entry))
        return self.memory

    def delete(self, memory_id: str) -> Memory:
        self.memory = Memory(
            tuple(entry for entry in self.memory.entries if entry.memory_id != memory_id)
        )
        return self.memory


def _automatic_service(
    store: RecordingMemoryStore,
    *,
    ids: list[str] | None = None,
) -> MemoryService:
    available_ids = iter(ids or ["memory_new"])
    return MemoryService(
        store,
        MemoryCoordinator(),
        clock=lambda: NOW,
        new_id=lambda: next(available_ids),
    )


def test_list_page_orders_event_or_record_time_descending_with_memory_id_tiebreaker() -> None:
    oldest = MemoryEntry(
        "memory_oldest",
        "older record",
        datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    same_time_b = MemoryEntry("memory_b", "same record b", NOW)
    same_time_a = MemoryEntry("memory_a", "same record a", NOW)
    future_event = MemoryEntry(
        "memory_event",
        "future event",
        datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        event_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
        event_timezone="Asia/Shanghai",
    )
    service = _automatic_service(
        RecordingMemoryStore(Memory((oldest, same_time_b, same_time_a, future_event)))
    )

    page = service.list_page(query="", cursor=None, limit=10)

    assert tuple(entry.memory_id for entry in page.entries) == (
        "memory_event",
        "memory_a",
        "memory_b",
        "memory_oldest",
    )


def test_list_page_continues_after_the_cursor_entry_is_deleted() -> None:
    latest = MemoryEntry("memory_latest", "latest", datetime(2026, 7, 17, 8, 0, tzinfo=UTC))
    cursor_entry = MemoryEntry("memory_cursor", "cursor", datetime(2026, 7, 16, 8, 0, tzinfo=UTC))
    oldest = MemoryEntry("memory_oldest", "oldest", datetime(2026, 7, 15, 8, 0, tzinfo=UTC))
    store = RecordingMemoryStore(Memory((oldest, cursor_entry, latest)))
    service = _automatic_service(store)

    first_page = service.list_page(query="", cursor=None, limit=2)
    service.delete_manual(cursor_entry.memory_id, revision=cursor_entry.revision)
    next_page = service.list_page(query="", cursor=first_page.next_cursor, limit=2)

    assert tuple(entry.memory_id for entry in first_page.entries) == ("memory_latest", "memory_cursor")
    assert tuple(entry.memory_id for entry in next_page.entries) == ("memory_oldest",)


def _operation(
    operation_type: MemoryOperationType,
    *,
    content: str | None = None,
    memory_id: str | None = None,
    revision: int | None = None,
    category: MemoryCategory | None = None,
    importance: int | None = None,
    original_time_text: str | None = None,
    event_at: datetime | None = None,
    event_timezone: str | None = None,
) -> MemoryOperation:
    return MemoryOperation(
        operation_type=operation_type,
        content=content,
        memory_id=memory_id,
        revision=revision,
        category=category,
        importance=importance,
        original_time_text=original_time_text,
        event_at=event_at,
        event_timezone=event_timezone,
    )


def test_apply_automatic_batch_adds_and_updates_with_one_replace() -> None:
    original = MemoryEntry(
        "memory_existing",
        "旧内容",
        created_at=NOW,
        category=MemoryCategory.EVENT,
        importance=2,
        source_kind=MemorySourceKind.MANUAL,
        revision=3,
    )
    store = RecordingMemoryStore(Memory((original,)))
    service = _automatic_service(store)
    operations = (
        _operation(
            MemoryOperationType.ADD,
            content="用户偏好中文回复",
            category=MemoryCategory.PREFERENCE,
            importance=5,
        ),
        _operation(
            MemoryOperationType.UPDATE,
            memory_id=original.memory_id,
            revision=3,
            content="新内容",
            category=MemoryCategory.EVENT,
            importance=4,
            original_time_text="明天",
            event_at=datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
            event_timezone="Asia/Shanghai",
        ),
        _operation(MemoryOperationType.NOOP),
    )

    changed = service.apply_automatic_batch(
        operations,
        source_session_id="session_1",
        source_message_id="message_9",
    )

    assert len(changed) == 2
    added, updated = changed
    assert added.memory_id == "memory_new"
    assert added.source_kind is MemorySourceKind.AUTOMATIC
    assert added.source_session_id == "session_1"
    assert added.source_message_id == "message_9"
    assert updated.memory_id == original.memory_id
    assert updated.created_at == original.created_at
    assert updated.revision == 4
    assert updated.source_kind is MemorySourceKind.AUTOMATIC
    assert updated.source_session_id == "session_1"
    assert updated.source_message_id == "message_9"
    assert store.load_count == 1
    assert store.replacements == [store.memory]
    assert store.memory.entries == (updated, added)


def test_apply_automatic_batch_stale_revision_discards_entire_batch_without_write() -> None:
    current = MemoryEntry(
        "memory_existing",
        "用户刚手动修改的内容",
        created_at=NOW,
        category=MemoryCategory.EVENT,
        revision=4,
    )
    store = RecordingMemoryStore(Memory((current,)))
    service = _automatic_service(store)
    operations = (
        _operation(
            MemoryOperationType.ADD,
            content="不能部分写入",
            category=MemoryCategory.OTHER,
            importance=3,
        ),
        _operation(
            MemoryOperationType.UPDATE,
            memory_id=current.memory_id,
            revision=3,
            content="抽取时看到的旧内容",
            category=MemoryCategory.EVENT,
            importance=3,
        ),
    )

    changed = service.apply_automatic_batch(
        operations,
        source_session_id="session_1",
        source_message_id="message_9",
    )

    assert changed == ()
    assert store.memory.entries == (current,)
    assert store.load_count == 1
    assert store.replacements == []


def test_apply_automatic_batch_rejects_raw_string_update_without_write() -> None:
    current = MemoryEntry(
        "memory_existing",
        "用户刚手动修改的内容",
        created_at=NOW,
        category=MemoryCategory.EVENT,
        revision=4,
    )
    store = RecordingMemoryStore(Memory((current,)))
    operation = _operation(
        cast(MemoryOperationType, "UPDATE"),
        memory_id=current.memory_id,
        revision=3,
        content="不能绕过冲突检查",
        category=MemoryCategory.EVENT,
        importance=3,
    )

    with pytest.raises(ValueError, match="operation type"):
        _automatic_service(store).apply_automatic_batch(
            (operation,),
            source_session_id="session_1",
            source_message_id="message_9",
        )

    assert store.memory.entries == (current,)
    assert store.replacements == []


def test_apply_automatic_batch_validates_later_delete_before_stale_drop() -> None:
    current = MemoryEntry(
        "memory_existing",
        "用户刚手动修改的内容",
        created_at=NOW,
        category=MemoryCategory.EVENT,
        revision=4,
    )
    store = RecordingMemoryStore(Memory((current,)))
    stale = _operation(
        MemoryOperationType.UPDATE,
        memory_id=current.memory_id,
        revision=3,
        content="抽取时看到的旧内容",
        category=MemoryCategory.EVENT,
        importance=3,
    )
    invalid_delete = _operation(cast(MemoryOperationType, "DELETE"))

    with pytest.raises(ValueError, match="operation type"):
        _automatic_service(store).apply_automatic_batch(
            (stale, invalid_delete),
            source_session_id="session_1",
            source_message_id="message_9",
        )

    assert store.memory.entries == (current,)
    assert store.replacements == []


def test_apply_automatic_batch_invalid_candidate_is_atomic_and_secret_safe() -> None:
    current = MemoryEntry("memory_existing", "原内容", NOW)
    store = RecordingMemoryStore(Memory((current,)))
    service = _automatic_service(store)
    secret = "sk-this-must-never-appear"
    operations = (
        _operation(
            MemoryOperationType.ADD,
            content="本来有效",
            category=MemoryCategory.OTHER,
            importance=3,
        ),
        _operation(
            MemoryOperationType.ADD,
            content=f"API key: {secret}",
            category=MemoryCategory.OTHER,
            importance=3,
        ),
    )

    with pytest.raises(ValueError, match="memory content is invalid") as exc_info:
        service.apply_automatic_batch(
            operations,
            source_session_id="session_1",
            source_message_id="message_9",
        )

    assert secret not in str(exc_info.value)
    assert store.memory.entries == (current,)
    assert store.replacements == []


def test_apply_automatic_batch_validates_every_operation_before_generating_ids() -> None:
    store = RecordingMemoryStore()
    generated_ids: list[str] = []

    def new_id() -> str:
        generated_ids.append(f"memory_generated_{len(generated_ids) + 1}")
        return generated_ids[-1]

    service = MemoryService(
        store,
        MemoryCoordinator(),
        clock=lambda: NOW,
        new_id=new_id,
    )
    operations = (
        _operation(
            MemoryOperationType.ADD,
            content="有效候选",
            category=MemoryCategory.OTHER,
            importance=3,
        ),
        _operation(
            MemoryOperationType.ADD,
            content="伪造枚举候选",
            category=cast(MemoryCategory, "not-a-category"),
            importance=3,
        ),
    )

    with pytest.raises(ValueError, match="automatic memory operation"):
        service.apply_automatic_batch(
            operations,
            source_session_id="session_1",
            source_message_id="message_9",
        )

    assert generated_ids == []
    assert store.replacements == []


def test_apply_automatic_batch_rejects_duplicate_update_targets_atomically() -> None:
    current = MemoryEntry("memory_existing", "原内容", NOW, revision=2)
    store = RecordingMemoryStore(Memory((current,)))
    service = _automatic_service(store)
    first = _operation(
        MemoryOperationType.UPDATE,
        memory_id=current.memory_id,
        revision=2,
        content="第一次",
        category=MemoryCategory.OTHER,
        importance=3,
    )

    with pytest.raises(ValueError, match="duplicate"):
        service.apply_automatic_batch(
            (first, replace(first, content="第二次")),
            source_session_id="session_1",
            source_message_id="message_9",
        )

    assert store.memory.entries == (current,)
    assert store.replacements == []


def test_apply_automatic_batch_noop_does_not_rewrite_store() -> None:
    current = MemoryEntry("memory_existing", "原内容", NOW)
    store = RecordingMemoryStore(Memory((current,)))

    changed = _automatic_service(store).apply_automatic_batch(
        (_operation(MemoryOperationType.NOOP),),
        source_session_id="session_1",
        source_message_id="message_9",
    )

    assert changed == ()
    assert store.load_count == 1
    assert store.replacements == []
