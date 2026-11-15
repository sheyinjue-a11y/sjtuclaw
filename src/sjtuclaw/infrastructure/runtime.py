"""Production implementations of deterministic runtime primitives."""

from datetime import UTC, datetime
from time import monotonic_ns
from uuid import uuid4


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ms(self) -> int:
        return monotonic_ns() // 1_000_000


class UuidIdGenerator:
    def new_id(self, prefix: str) -> str:
        normalized = prefix.strip().rstrip("_")
        if not normalized:
            raise ValueError("prefix must not be empty")
        return f"{normalized}_{uuid4().hex}"
