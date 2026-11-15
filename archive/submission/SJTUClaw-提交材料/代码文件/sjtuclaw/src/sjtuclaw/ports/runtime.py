"""Deterministic runtime primitive ports."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ms(self) -> int: ...


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str: ...
