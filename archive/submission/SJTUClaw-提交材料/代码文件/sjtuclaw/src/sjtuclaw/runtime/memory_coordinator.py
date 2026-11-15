"""Process-local serialization for cross-session memory operations."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock


class MemoryCoordinator:
    def __init__(self) -> None:
        self._lock = RLock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock:
            yield
