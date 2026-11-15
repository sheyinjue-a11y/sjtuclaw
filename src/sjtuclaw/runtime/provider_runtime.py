"""Atomically switch model providers while keeping active runs stable."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from sjtuclaw.config import ProviderConfiguration, TokenCounterMode
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMStreamEvent
from sjtuclaw.ports.llm import StreamingLLMClient
from sjtuclaw.ports.token_counter import TokenCounter


class ClosableStreamingLLMClient(StreamingLLMClient, Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _ProviderLease:
    configuration: ProviderConfiguration
    client: ClosableStreamingLLMClient
    token_counter: TokenCounter
    token_counter_mode: TokenCounterMode


@dataclass(frozen=True, slots=True)
class PreparedProvider:
    """Fully constructed future lease awaiting persistence and activation."""

    lease: _ProviderLease


class ProviderRuntime:
    """A task-local lease over the provider selected for a complete run."""

    def __init__(
        self,
        configuration: ProviderConfiguration,
        *,
        create_client: Callable[[ProviderConfiguration], ClosableStreamingLLMClient],
        create_token_counter: Callable[[ProviderConfiguration], TokenCounter],
    ) -> None:
        self._create_client = create_client
        self._create_token_counter = create_token_counter
        self._active = self._make_lease(configuration)
        self._lease: ContextVar[_ProviderLease | None] = ContextVar(
            "provider_runtime_lease",
            default=None,
        )
        self._clients: list[ClosableStreamingLLMClient] = [self._active.client]

    @property
    def configuration(self) -> ProviderConfiguration:
        return self._active.configuration

    @property
    def token_counter_mode(self) -> TokenCounterMode:
        return self._current_lease().token_counter_mode

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[None]:
        token = self._lease.set(self._active)
        try:
            yield
        finally:
            self._lease.reset(token)

    def replace(self, configuration: ProviderConfiguration) -> None:
        """Install a configuration for future leases without mutating active ones."""

        lease = self._make_lease(configuration)
        self._active = lease
        self._clients.append(lease.client)

    async def prepare(self, configuration: ProviderConfiguration) -> PreparedProvider:
        """Construct every fallible runtime dependency without switching active state."""

        client = self._create_client(configuration)
        try:
            token_counter = self._create_token_counter(configuration)
        except Exception:
            await client.aclose()
            raise
        return PreparedProvider(
            self._lease_from_parts(configuration, client, token_counter)
        )

    def activate(self, prepared: PreparedProvider) -> None:
        """Publish an already prepared lease for future runs."""

        self._active = prepared.lease
        self._clients.append(prepared.lease.client)

    async def discard(self, prepared: PreparedProvider) -> None:
        """Release a prepared client after persistence failed."""

        await prepared.lease.client.aclose()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await self._current_lease().client.complete(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        client = self._current_lease().client
        if isinstance(client, StreamingLLMClient):
            return client.stream(request)
        return _complete_as_stream(client, request)

    def count(self, request: LLMRequest) -> int:
        return self._current_lease().token_counter.count(request)

    async def aclose(self) -> None:
        await _close_clients(self._clients)
        self._clients.clear()

    def _current_lease(self) -> _ProviderLease:
        return self._lease.get() or self._active

    def _make_lease(self, configuration: ProviderConfiguration) -> _ProviderLease:
        return self._lease_from_parts(
            configuration,
            self._create_client(configuration),
            self._create_token_counter(configuration),
        )

    @staticmethod
    def _lease_from_parts(
        configuration: ProviderConfiguration,
        client: ClosableStreamingLLMClient,
        token_counter: TokenCounter,
    ) -> _ProviderLease:
        mode: TokenCounterMode = (
            "deepseek" if configuration.provider.value == "deepseek" else "approximate"
        )
        return _ProviderLease(
            configuration=configuration,
            client=client,
            token_counter=token_counter,
            token_counter_mode=mode,
        )


async def _close_clients(clients: list[ClosableStreamingLLMClient]) -> None:
    for client in clients:
        await client.aclose()


async def _complete_as_stream(
    client: ClosableStreamingLLMClient,
    request: LLMRequest,
) -> AsyncIterator[LLMStreamEvent]:
    yield LLMStreamEvent(response=await client.complete(request))
