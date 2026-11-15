"""Token counting contract independent of tokenizer implementation."""

from typing import Protocol

from sjtuclaw.models.llm import LLMRequest


class TokenCounter(Protocol):
    def count(self, request: LLMRequest) -> int: ...
