from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sjtuclaw.config import ProviderConfiguration, ProviderKind
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMUsage
from sjtuclaw.runtime.provider_runtime import ProviderRuntime


@dataclass
class _Client:
    name: str
    closed: bool = False

    async def complete(self, _: LLMRequest) -> LLMResponse:
        return LLMResponse(self.name, LLMUsage(1, 1, 2), "stop")

    async def aclose(self) -> None:
        self.closed = True


class _Counter:
    def count(self, _: LLMRequest) -> int:
        return 10


def _configuration(model: str) -> ProviderConfiguration:
    return ProviderConfiguration(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        model=model,
        base_url="https://api.example/v1",
        api_key="secret",
    )


def test_provider_switch_only_affects_new_leases() -> None:
    created: list[_Client] = []

    def create_client(configuration: ProviderConfiguration) -> _Client:
        client = _Client(configuration.model)
        created.append(client)
        return client

    runtime = ProviderRuntime(
        _configuration("first"),
        create_client=create_client,
        create_token_counter=lambda _: _Counter(),
    )
    request = LLMRequest.__new__(LLMRequest)

    async def exercise() -> tuple[str, str]:
        async with runtime.lease():
            runtime.replace(_configuration("second"))
            old = await runtime.complete(request)
        async with runtime.lease():
            new = await runtime.complete(request)
        return old.content, new.content

    assert asyncio.run(exercise()) == ("first", "second")
    assert runtime.token_counter_mode == "approximate"
    assert len(created) == 2


def test_provider_prepare_failure_closes_new_client_and_keeps_active_configuration() -> None:
    created: list[_Client] = []

    def create_client(configuration: ProviderConfiguration) -> _Client:
        client = _Client(configuration.model)
        created.append(client)
        return client

    def create_counter(configuration: ProviderConfiguration) -> _Counter:
        if configuration.model == "broken":
            raise RuntimeError("tokenizer failed")
        return _Counter()

    runtime = ProviderRuntime(
        _configuration("first"),
        create_client=create_client,
        create_token_counter=create_counter,
    )

    async def exercise() -> None:
        try:
            await runtime.prepare(_configuration("broken"))
        except RuntimeError as error:
            assert str(error) == "tokenizer failed"
        else:
            raise AssertionError("prepare must fail")

    asyncio.run(exercise())

    assert runtime.configuration == _configuration("first")
    assert [client.name for client in created] == ["first", "broken"]
    assert created[0].closed is False
    assert created[1].closed is True


def test_provider_discard_closes_prepared_client_without_switching_active_configuration() -> None:
    created: list[_Client] = []

    def create_client(configuration: ProviderConfiguration) -> _Client:
        client = _Client(configuration.model)
        created.append(client)
        return client

    runtime = ProviderRuntime(
        _configuration("first"),
        create_client=create_client,
        create_token_counter=lambda _: _Counter(),
    )

    async def exercise() -> None:
        prepared = await runtime.prepare(_configuration("second"))
        await runtime.discard(prepared)

    asyncio.run(exercise())

    assert runtime.configuration == _configuration("first")
    assert created[1].closed is True
