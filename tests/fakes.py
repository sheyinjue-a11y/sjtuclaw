"""Deterministic test implementations shared across test modules."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from sjtuclaw.models.llm import LLMRequest, LLMResponse


@dataclass
class FakeClock:
    current: datetime
    elapsed_ms: int = 0

    def now(self) -> datetime:
        return self.current

    def monotonic_ms(self) -> int:
        return self.elapsed_ms


@dataclass
class FakeIdGenerator:
    next_value: int = 1

    def new_id(self, prefix: str) -> str:
        value = f"{prefix}_{self.next_value:03d}"
        self.next_value += 1
        return value


@dataclass
class FakeLLMClient:
    responses: list[LLMResponse]
    delay_seconds: float = 0
    requests: list[LLMRequest] = field(default_factory=list)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if not self.responses:
            raise AssertionError("FakeLLMClient has no remaining response")
        return self.responses.pop(0)
