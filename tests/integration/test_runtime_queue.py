from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMUsage
from sjtuclaw.models.runtime import InvocationSource, RuntimeEvent, RuntimeRequest, UserInput
from sjtuclaw.models.session import Session
from sjtuclaw.runtime import AgentRuntime, ContextBuilder, RunHandle, SessionCoordinator
from tests.fakes import FakeClock, FakeIdGenerator, FakeLLMClient


def test_same_session_requests_are_complete_and_ordered() -> None:
    asyncio.run(_assert_same_session_requests_are_complete_and_ordered())


async def _assert_same_session_requests_are_complete_and_ordered() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", now, now))
    llm = FakeLLMClient(
        [
            LLMResponse("first answer", LLMUsage(1, 1, 2), "stop"),
            LLMResponse("second answer", LLMUsage(3, 1, 4), "stop"),
        ],
        delay_seconds=0.01,
    )
    runtime = AgentRuntime(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(now),
        ids=FakeIdGenerator(),
    )
    first = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("first question"),
    )
    second = RuntimeRequest(
        "request_002",
        "session_001",
        InvocationSource.GATEWAY,
        UserInput("second question"),
    )

    first_handle = await runtime.submit(first)
    second_handle = await runtime.submit(second)
    first_events, second_events = await asyncio.gather(
        _drain(first_handle), _drain(second_handle)
    )

    assert [message.content for message in sessions.load("session_001").messages] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]
    assert [request.messages[-1].content for request in llm.requests] == [
        "first question",
        "second question",
    ]
    assert [message.content for message in llm.requests[1].messages[-3:]] == [
        "first question",
        "first answer",
        "second question",
    ]
    assert {event.sequence_number for event in first_events} == {1}
    assert {event.sequence_number for event in second_events} == {2}


def test_different_sessions_can_complete_independently() -> None:
    asyncio.run(_assert_different_sessions_can_complete_independently())


async def _assert_different_sessions_can_complete_independently() -> None:
    class BlockingFirstLLM:
        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def complete(self, request: LLMRequest) -> LLMResponse:
            prompt = request.messages[-1].content
            if prompt == "block first session":
                self.first_started.set()
                await self.release_first.wait()
            return LLMResponse(f"answer to {prompt}", LLMUsage(1, 1, 2), "stop")

    now = datetime(2026, 7, 15, tzinfo=UTC)
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "first", (), "", now, now))
    sessions.create(Session("session_002", "second", (), "", now, now))
    llm = BlockingFirstLLM()
    runtime = AgentRuntime(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(now),
        ids=FakeIdGenerator(),
    )

    first = await runtime.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.CLI,
            UserInput("block first session"),
        )
    )
    await llm.first_started.wait()
    second = await runtime.submit(
        RuntimeRequest(
            "request_002",
            "session_002",
            InvocationSource.GATEWAY,
            UserInput("finish second session"),
        )
    )

    await asyncio.wait_for(_drain(second), timeout=0.5)
    assert sessions.load("session_002").messages[-1].content == (
        "answer to finish second session"
    )

    llm.release_first.set()
    await _drain(first)


async def _drain(handle: RunHandle) -> list[RuntimeEvent]:
    events: list[RuntimeEvent] = []
    async for event in handle.events():
        events.append(event)
    return events
