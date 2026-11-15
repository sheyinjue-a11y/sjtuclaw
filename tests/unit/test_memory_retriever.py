from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sjtuclaw.errors import StoreError
from sjtuclaw.models.llm import LLMRequest
from sjtuclaw.models.memory import Memory, MemoryCategory, MemoryEntry
from sjtuclaw.runtime.memory_retriever import LocalMemoryRetriever

NOW = datetime(2026, 7, 28, tzinfo=UTC)


@dataclass
class StaticMemoryStore:
    memory: Memory

    def load(self) -> Memory:
        return self.memory

    def replace(self, memory: Memory) -> Memory:
        self.memory = memory
        return memory

    def add(self, entry: MemoryEntry) -> Memory:
        return self.replace(Memory((*self.memory.entries, entry)))

    def delete(self, memory_id: str) -> Memory:
        return self.replace(
            Memory(tuple(entry for entry in self.memory.entries if entry.memory_id != memory_id))
        )


@dataclass
class CharacterTokenCounter:
    requests: list[LLMRequest] = field(default_factory=list)

    def count(self, request: LLMRequest) -> int:
        self.requests.append(request)
        return sum(len(message.content) for message in request.messages)


def _entry(
    memory_id: str,
    content: str,
    *,
    category: MemoryCategory = MemoryCategory.EVENT,
    importance: int = 3,
    updated_at: datetime = NOW,
) -> MemoryEntry:
    return MemoryEntry(
        memory_id,
        content,
        created_at=NOW - timedelta(days=365),
        category=category,
        importance=importance,
        updated_at=updated_at,
    )


def _retriever(
    entries: tuple[MemoryEntry, ...],
    *,
    limit: int = 8,
    max_tokens: int = 2_048,
    counter: CharacterTokenCounter | None = None,
) -> LocalMemoryRetriever:
    return LocalMemoryRetriever(
        StaticMemoryStore(Memory(entries)),
        counter or CharacterTokenCounter(),
        limit=limit,
        max_tokens=max_tokens,
    )


def test_retrieve_ranks_cjk_bigram_overlap() -> None:
    retriever = _retriever(
        (
            _entry("memory_001", "海鲜餐厅在海边"),
            _entry("memory_002", "露营需要带防雨帐篷"),
        )
    )

    result = retriever.retrieve("周末露营地点")

    assert [entry.memory_id for entry in result] == ["memory_002", "memory_001"]


def test_retrieve_ranks_latin_and_numeric_tokens() -> None:
    retriever = _retriever(
        (
            _entry("memory_001", "Apollo archive from 2024"),
            _entry("memory_002", "Apollo 2026 project deadline is Friday"),
        )
    )

    result = retriever.retrieve("Apollo project 2026")

    assert [entry.memory_id for entry in result] == ["memory_002", "memory_001"]


def test_retrieve_gives_complete_substring_more_weight_than_token_overlap() -> None:
    retriever = _retriever(
        (
            _entry("memory_001", "deploy docs before staging review"),
            _entry("memory_002", "deploy staging after approval"),
        )
    )

    result = retriever.retrieve("deploy staging")

    assert [entry.memory_id for entry in result] == ["memory_002", "memory_001"]


def test_retrieve_biases_stable_categories_when_relevance_is_equal() -> None:
    retriever = _retriever(
        (
            _entry("memory_001", "plan details", category=MemoryCategory.EVENT),
            _entry("memory_002", "plan details", category=MemoryCategory.CONSTRAINT),
        )
    )

    result = retriever.retrieve("plan")

    assert [entry.memory_id for entry in result] == ["memory_002", "memory_001"]


def test_retrieve_orders_equal_matches_by_importance_recency_updated_at_then_id() -> None:
    retriever = _retriever(
        (
            _entry("memory_004", "release", importance=3, updated_at=NOW - timedelta(days=2)),
            _entry("memory_003", "release", importance=3, updated_at=NOW - timedelta(days=2)),
            _entry("memory_002", "release", importance=3, updated_at=NOW - timedelta(days=1)),
            _entry("memory_001", "release", importance=5, updated_at=NOW - timedelta(days=365)),
        )
    )

    result = retriever.retrieve("release")

    assert [entry.memory_id for entry in result] == [
        "memory_001",
        "memory_002",
        "memory_003",
        "memory_004",
    ]


def test_retrieve_limits_results_to_top_k() -> None:
    entries = tuple(_entry(f"memory_{index:03d}", "release") for index in range(10))

    result = _retriever(entries, limit=8).retrieve("release")

    assert len(result) == 8
    assert [entry.memory_id for entry in result] == [
        f"memory_{index:03d}" for index in range(8)
    ]


def test_retrieve_stops_before_full_stable_memory_exceeds_token_budget() -> None:
    counter = CharacterTokenCounter()
    entries = (
        _entry("memory_001", "first"),
        _entry("memory_002", "second"),
    )
    one_entry_text = "Long-term memory:\n- [memory_001] first"

    result = _retriever(
        entries,
        max_tokens=len(one_entry_text),
        counter=counter,
    ).retrieve("first second")

    assert [entry.memory_id for entry in result] == ["memory_001"]
    assert all(len(request.messages) == 1 for request in counter.requests)
    assert all(request.messages[0].role.value == "system" for request in counter.requests)
    assert counter.requests[-1].messages[0].content == (
        "Long-term memory:\n- [memory_001] first\n- [memory_002] second"
    )


def test_retrieve_empty_query_returns_only_few_high_priority_stable_memories() -> None:
    retriever = _retriever(
        (
            _entry("memory_001", "event", importance=5),
            _entry("memory_002", "project", category=MemoryCategory.PROJECT, importance=5),
            _entry("memory_003", "identity", category=MemoryCategory.IDENTITY, importance=5),
            _entry("memory_004", "preference", category=MemoryCategory.PREFERENCE, importance=4),
            _entry("memory_005", "constraint", category=MemoryCategory.CONSTRAINT, importance=3),
            _entry("memory_006", "older identity", category=MemoryCategory.IDENTITY, importance=1),
        )
    )

    result = retriever.retrieve("")

    assert [entry.memory_id for entry in result] == ["memory_003", "memory_004", "memory_005"]


def test_retrieve_returns_empty_tuple_when_store_load_fails() -> None:
    class FailingStore:
        def load(self) -> Memory:
            raise StoreError("broken memory file")

        def replace(self, memory: Memory) -> Memory:
            raise AssertionError("store must not be replaced")

        def add(self, entry: MemoryEntry) -> Memory:
            raise AssertionError("store must not add")

        def delete(self, memory_id: str) -> Memory:
            raise AssertionError("store must not delete")

    retriever = LocalMemoryRetriever(FailingStore(), CharacterTokenCounter())

    assert retriever.retrieve("anything") == ()
