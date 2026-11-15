"""Backend-neutral contract tests for MemoryStore implementations."""

from datetime import UTC, datetime, timedelta

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.models.memory import Memory, MemoryCategory, MemoryEntry, MemorySourceKind
from sjtuclaw.ports.stores import MemoryStore

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


class MemoryStoreContract:
    """Mixin inherited by every concrete MemoryStore contract suite."""

    def test_missing_store_loads_empty_memory(self, store: MemoryStore) -> None:
        assert store.load().entries == ()

    def test_replace_load_delete_preserves_order(self, store: MemoryStore) -> None:
        first = MemoryEntry("memory_001", "第一条", NOW)
        second = MemoryEntry("memory_002", "第二条", NOW + timedelta(seconds=1))

        assert store.replace(Memory((first, second))).entries == (first, second)
        assert store.load().entries == (first, second)
        assert store.delete(first.memory_id).entries == (second,)
        assert store.load().entries == (second,)

    def test_duplicate_add_fails_without_replacing_memory(self, store: MemoryStore) -> None:
        original = MemoryEntry("memory_001", "原内容", NOW)
        store.add(original)

        with pytest.raises(StoreError):
            store.add(MemoryEntry("memory_001", "冲突内容", NOW))

        assert store.load().entries == (original,)

    def test_delete_missing_entry_fails_without_changing_memory(
        self,
        store: MemoryStore,
    ) -> None:
        original = MemoryEntry("memory_001", "原内容", NOW)
        store.add(original)

        with pytest.raises(StoreError):
            store.delete("missing")

        assert store.load().entries == (original,)

    def test_replace_rejects_duplicate_ids_without_changing_memory(self, store: MemoryStore) -> None:
        original = MemoryEntry("memory_001", "原内容", NOW)
        store.add(original)

        duplicate = MemoryEntry(
            "memory_001",
            "冲突内容",
            NOW + timedelta(seconds=1),
            category=MemoryCategory.PROJECT,
            source_kind=MemorySourceKind.MANUAL,
        )
        with pytest.raises(StoreError, match="duplicate"):
            store.replace(Memory((original, duplicate)))

        assert store.load().entries == (original,)
