"""OpenAI Chat Completions adapter for compatible model providers."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from sjtuclaw.config import ProviderConfiguration, Settings
from sjtuclaw.infrastructure.llm.deepseek import DeepSeekLLMClient
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMStreamEvent
from sjtuclaw.ports.runtime import IdGenerator


class OpenAICompatibleLLMClient:
    """Reuse the established Chat Completions behavior for compatible APIs."""

    def __init__(
        self,
        configuration: ProviderConfiguration,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._delegate = DeepSeekLLMClient(
            Settings(
                api_key=configuration.api_key,
                base_url=configuration.base_url,
                model=configuration.model,
            ),
            client=client,
            transport=transport,
            ids=ids,
            thinking_enabled=None,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await self._delegate.complete(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._delegate.stream(request)

    async def aclose(self) -> None:
        await self._delegate.aclose()
