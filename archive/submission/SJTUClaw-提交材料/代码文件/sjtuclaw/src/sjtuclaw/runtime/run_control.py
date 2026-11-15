"""In-memory control channel for one active Run."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SteerCommand:
    steer_id: str
    idempotency_key: str
    text: str

    def __post_init__(self) -> None:
        if not self.steer_id or not self.idempotency_key or not self.text.strip():
            raise ValueError("steer command fields must not be empty")


class RunControl:
    """Accept at most one pending steer and expose it at AgentLoop safe points."""

    def __init__(self, *, max_steers: int = 5) -> None:
        if max_steers < 1:
            raise ValueError("max_steers must be positive")
        self._max_steers = max_steers
        self._lock = asyncio.Lock()
        self._pending: SteerCommand | None = None
        self._accepted: dict[tuple[str, str], str] = {}
        self._pending_event = asyncio.Event()

    async def request(self, command: SteerCommand) -> str:
        return await self.request_after(command, lambda: None)

    async def accepted_result(self, steer_id: str, idempotency_key: str) -> str | None:
        """Return an earlier result without requiring the consumed source text."""

        async with self._lock:
            return self._accepted.get((steer_id, idempotency_key))

    async def request_after(
        self,
        command: SteerCommand,
        commit: Callable[[], None],
    ) -> str:
        """Commit durable source removal before exposing accepted guidance."""

        key = (command.steer_id, command.idempotency_key)
        async with self._lock:
            previous = self._accepted.get(key)
            if previous is not None:
                return previous
            if len(self._accepted) >= self._max_steers:
                return "STEER_LIMIT_EXCEEDED"
            if self._pending is not None:
                return "STEER_ALREADY_PENDING"
            commit()
            self._pending = command
            self._accepted[key] = "STEER_ACCEPTED"
            self._pending_event.set()
            return "STEER_ACCEPTED"

    async def wait_for_pending(self) -> None:
        await self._pending_event.wait()

    async def take_pending(self) -> SteerCommand | None:
        async with self._lock:
            command = self._pending
            self._pending = None
            self._pending_event.clear()
            return command

    def has_pending(self) -> bool:
        return self._pending_event.is_set()
