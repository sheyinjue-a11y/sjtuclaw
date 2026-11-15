"""Deterministic local retrieval for bounded long-term memory context."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from math import exp
from unicodedata import normalize

from sjtuclaw.errors import StoreError
from sjtuclaw.models.llm import LLMRequest
from sjtuclaw.models.memory import MemoryCategory, MemoryEntry
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.ports.stores import MemoryStore
from sjtuclaw.ports.token_counter import TokenCounter

_LATIN_OR_NUMERIC_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_STABLE_CATEGORIES = frozenset(
    {
        MemoryCategory.IDENTITY,
        MemoryCategory.PREFERENCE,
        MemoryCategory.CONSTRAINT,
    }
)
_EMPTY_QUERY_LIMIT = 3
_RELEVANT_SUBSTRING_WEIGHT = 10_000.0
_LATIN_TOKEN_WEIGHT = 100.0
_CJK_BIGRAM_WEIGHT = 75.0
_STABLE_CATEGORY_WEIGHT = 25.0
_IMPORTANCE_WEIGHT = 100.0
_RECENCY_WEIGHT = 20.0
_RECENCY_HALF_LIFE_DAYS = 90.0
_TOKEN_COUNT_TIMESTAMP = datetime(1970, 1, 1, tzinfo=UTC)


class LocalMemoryRetriever:
    """Ranks persisted memory without network calls or nondeterministic state."""

    def __init__(
        self,
        store: MemoryStore,
        token_counter: TokenCounter,
        *,
        limit: int = 8,
        max_tokens: int = 2_048,
    ) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        self._store = store
        self._token_counter = token_counter
        self._limit = limit
        self._max_tokens = max_tokens

    def retrieve(self, query: str) -> tuple[MemoryEntry, ...]:
        try:
            entries = self._store.load().entries
        except StoreError:
            return ()

        normalized_query = _normalize(query)
        if not normalized_query:
            ranked = self._rank_empty_query(entries)
        else:
            ranked = self._rank_query(entries, normalized_query)
        return self._within_token_budget(ranked)

    def _rank_query(
        self,
        entries: tuple[MemoryEntry, ...],
        normalized_query: str,
    ) -> tuple[MemoryEntry, ...]:
        query_latin_tokens = frozenset(_LATIN_OR_NUMERIC_TOKEN.findall(normalized_query))
        query_cjk_bigrams = _cjk_bigrams(normalized_query)
        latest = max((_updated_at(entry) for entry in entries), default=_TOKEN_COUNT_TIMESTAMP)

        def order(entry: MemoryEntry) -> tuple[float, datetime, str]:
            content = _normalize(entry.content)
            latin_matches = len(query_latin_tokens & frozenset(_LATIN_OR_NUMERIC_TOKEN.findall(content)))
            cjk_matches = len(query_cjk_bigrams & _cjk_bigrams(content))
            substring_score = (
                _RELEVANT_SUBSTRING_WEIGHT if normalized_query in content else 0.0
            )
            stable_score = (
                _STABLE_CATEGORY_WEIGHT if entry.category in _STABLE_CATEGORIES else 0.0
            )
            updated_at = _updated_at(entry)
            recency_days = max((latest - updated_at).total_seconds() / 86_400, 0.0)
            recency_score = _RECENCY_WEIGHT * exp(
                -recency_days / _RECENCY_HALF_LIFE_DAYS
            )
            score = (
                substring_score
                + latin_matches * _LATIN_TOKEN_WEIGHT
                + cjk_matches * _CJK_BIGRAM_WEIGHT
                + stable_score
                + entry.importance * _IMPORTANCE_WEIGHT
                + recency_score
            )
            return (-score, updated_at, entry.memory_id)

        return tuple(
            sorted(
                entries,
                key=lambda entry: (*order(entry)[:1], -_updated_at(entry).timestamp(), entry.memory_id),
            )
        )

    def _rank_empty_query(self, entries: tuple[MemoryEntry, ...]) -> tuple[MemoryEntry, ...]:
        stable_entries = tuple(entry for entry in entries if entry.category in _STABLE_CATEGORIES)
        latest = max(
            (_updated_at(entry) for entry in stable_entries),
            default=_TOKEN_COUNT_TIMESTAMP,
        )

        def order(entry: MemoryEntry) -> tuple[float, datetime, str]:
            updated_at = _updated_at(entry)
            recency_days = max((latest - updated_at).total_seconds() / 86_400, 0.0)
            recency_score = _RECENCY_WEIGHT * exp(
                -recency_days / _RECENCY_HALF_LIFE_DAYS
            )
            score = entry.importance * _IMPORTANCE_WEIGHT + recency_score
            return (-score, updated_at, entry.memory_id)

        ranked = sorted(
            stable_entries,
            key=lambda entry: (*order(entry)[:1], -_updated_at(entry).timestamp(), entry.memory_id),
        )
        return tuple(ranked[:_EMPTY_QUERY_LIMIT])

    def _within_token_budget(self, ranked: tuple[MemoryEntry, ...]) -> tuple[MemoryEntry, ...]:
        selected: list[MemoryEntry] = []
        for entry in ranked[: self._limit]:
            candidate = (*selected, entry)
            if self._token_count(candidate) <= self._max_tokens:
                selected.append(entry)
        return tuple(selected)

    def _token_count(self, entries: tuple[MemoryEntry, ...]) -> int:
        content = _stable_memory_content(entries)
        request = LLMRequest(
            messages=(
                Message(
                    message_id="stable_memory",
                    role=MessageRole.SYSTEM,
                    content=content,
                    created_at=_TOKEN_COUNT_TIMESTAMP,
                    sequence_number=0,
                ),
            )
        )
        return self._token_counter.count(request)


def _normalize(value: str) -> str:
    return normalize("NFKC", value).casefold()


def _updated_at(entry: MemoryEntry) -> datetime:
    updated_at = entry.updated_at
    if updated_at is None:
        raise RuntimeError("MemoryEntry.updated_at must be set after initialization")
    return updated_at


def _cjk_bigrams(value: str) -> frozenset[str]:
    return frozenset(
        run[index : index + 2]
        for run in _CJK_RUN.findall(value)
        for index in range(len(run) - 1)
    )


def _stable_memory_content(entries: tuple[MemoryEntry, ...]) -> str:
    return "Long-term memory:\n" + "\n".join(
        f"- [{entry.memory_id}] {entry.content}" for entry in entries
    )
