"""Interfaces and values for editable long-term-memory policy."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from unicodedata import category

from sjtuclaw.models.memory import MemoryEntry

MAX_MEMORY_POLICY_RULES_LENGTH = 12_000


class MemoryRetriever(Protocol):
    """Returns bounded memories relevant to one current user query."""

    def retrieve(self, query: str) -> tuple[MemoryEntry, ...]: ...


def validate_memory_policy_rules(rules: str) -> str:
    """Reject policy text that cannot safely be persisted and prompted."""
    if (
        type(rules) is not str
        or not rules.strip()
        or len(rules) > MAX_MEMORY_POLICY_RULES_LENGTH
        or any(
            category(character) == "Cc" and character not in "\n\r\t" for character in rules
        )
    ):
        raise ValueError("memory policy rules are invalid")
    return rules


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    rules: str
    revision: int
    updated_at: datetime

    def __post_init__(self) -> None:
        validate_memory_policy_rules(self.rules)
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("memory policy revision must be a positive integer")
        if (
            not isinstance(self.updated_at, datetime)
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
            or self.updated_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("memory policy updated_at must be timezone-aware UTC")


class MemoryPolicyStore(Protocol):
    def load(self) -> MemoryPolicy | None: ...

    def save(self, policy: MemoryPolicy) -> MemoryPolicy: ...

    def reset(self) -> None: ...
