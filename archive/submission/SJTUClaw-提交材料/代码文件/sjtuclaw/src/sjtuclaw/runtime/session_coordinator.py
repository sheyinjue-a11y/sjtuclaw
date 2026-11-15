"""Per-session ordering for all current and future entrypoints."""

import asyncio


class SessionCoordinator:
    def __init__(self) -> None:
        self._execution_locks: dict[str, asyncio.Lock] = {}
        self._admission_locks: dict[str, asyncio.Lock] = {}
        self._next_invocation_sequence: dict[str, int] = {}

    async def reserve_invocation_sequence(self, session_id: str) -> int:
        if not session_id:
            raise ValueError("session_id must not be empty")
        lock = self._admission_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._admission_locks[session_id] = lock
        async with lock:
            sequence = self._next_invocation_sequence.get(session_id, 1)
            self._next_invocation_sequence[session_id] = sequence + 1
            return sequence

    def admission_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._admission_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._admission_locks[session_id] = lock
        return lock

    def execution_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._execution_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._execution_locks[session_id] = lock
        return lock
