"""Purpose-bound, one-time Gateway bootstrap tokens."""

import secrets
import time
from collections.abc import Callable
from typing import Literal

BootstrapPurpose = Literal["browser", "pet"]


class BootstrapTokenRegistry:
    """Issue and consume short-lived, purpose-bound bootstrap tokens."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._tokens: dict[str, tuple[BootstrapPurpose, float]] = {}

    def issue(
        self,
        purpose: BootstrapPurpose,
        *,
        ttl_seconds: float = 30.0,
        token: str | None = None,
    ) -> str:
        value = token or secrets.token_urlsafe(32)
        if len(value) < 43 or ttl_seconds <= 0:
            raise ValueError("bootstrap token and TTL must be valid")
        self._tokens[value] = (purpose, self._now() + ttl_seconds)
        return value

    def consume(self, token: str) -> BootstrapPurpose | None:
        record = self._tokens.pop(token, None)
        if record is None or record[1] <= self._now():
            return None
        return record[0]
