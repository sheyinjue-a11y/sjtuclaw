from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMUsage
from sjtuclaw.models.runtime import InvocationSource, RuntimeEventType, RuntimeRequest, UserInput
from sjtuclaw.models.session import Session
from sjtuclaw.runtime import AgentRuntime, ContextBuilder, SessionCoordinator, SessionService
from tests.fakes import FakeClock, FakeIdGenerator

NOW = datetime(2026, 7, 15, tzinfo=UTC)


class BlockingLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.started.set()
        await self.release.wait()
        return LLMResponse("answer", LLMUsage(2, 1, 3), "stop")


def _build() -> tuple[AgentRuntime, SessionService, InMemorySessionStore, BlockingLLM]:
    stores = InMemorySessionStore()
    stores.create(Session("session_001", "original", (), "", NOW, NOW))
    coordinator = SessionCoordinator()
    clock = FakeClock(NOW)
    ids = FakeIdGenerator()
    llm = BlockingLLM()
    runtime = AgentRuntime(
        llm=llm,
        sessions=stores,
        context_builder=ContextBuilder("system"),
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    service = SessionService(
        sessions=stores,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    return runtime, service, stores, llm


def test_rename_waits_for_active_turn_and_keeps_new_messages() -> None:
    asyncio.run(_assert_rename_waits_for_active_turn_and_keeps_new_messages())


async def _assert_rename_waits_for_active_turn_and_keeps_new_messages() -> None:
    runtime, service, stores, llm = _build()
    handle = await runtime.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("question"),
        )
    )
    await llm.started.wait()
    rename_task = asyncio.create_task(service.rename("session_001", "renamed"))
    await asyncio.sleep(0)
    assert not rename_task.done()

    llm.release.set()
    events = [event async for event in handle.events()]
    renamed = await rename_task

    assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED
    assert renamed.title == "renamed"
    assert [message.content for message in stores.load("session_001").messages] == [
        "question",
        "answer",
    ]
    await runtime.aclose()


def test_delete_waits_for_active_turn_instead_of_breaking_final_save() -> None:
    asyncio.run(_assert_delete_waits_for_active_turn_instead_of_breaking_final_save())


async def _assert_delete_waits_for_active_turn_instead_of_breaking_final_save() -> None:
    runtime, service, stores, llm = _build()
    handle = await runtime.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("question"),
        )
    )
    await llm.started.wait()
    delete_task = asyncio.create_task(service.delete("session_001"))
    await asyncio.sleep(0)
    assert not delete_task.done()

    llm.release.set()
    events = [event async for event in handle.events()]
    await delete_task

    assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED
    with pytest.raises(StoreError, match="session not found"):
        stores.load("session_001")
    await runtime.aclose()
