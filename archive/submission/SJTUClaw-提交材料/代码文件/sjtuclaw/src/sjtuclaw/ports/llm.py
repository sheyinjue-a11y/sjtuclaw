"""Language model contracts independent of provider and transport details."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMStreamEvent


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


@runtime_checkable
class StreamingLLMClient(LLMClient, Protocol):
    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]: ...
