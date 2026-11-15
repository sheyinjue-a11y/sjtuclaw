from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest

from sjtuclaw.errors import LLMError
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMUsage
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.runtime import (
    InvocationSource,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRequest,
    UserInput,
)
from sjtuclaw.models.session import Session
from sjtuclaw.runtime import (
    AgentRuntime,
    CompactionService,
    ContextBuilder,
    RunHandle,
    SessionCoordinator,
)
from tests.fakes import FakeClock, FakeIdGenerator, FakeLLMClient

NOW = datetime(2026, 7, 15, tzinfo=UTC)


def test_runtime_persists_user_before_assistant_and_emits_usage() -> None:
    asyncio.run(_assert_runtime_persists_user_before_assistant_and_emits_usage())


async def _assert_runtime_persists_user_before_assistant_and_emits_usage() -> None:
    class RecordingStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.saved_message_contents: list[list[str]] = []

        def save(self, session: Session) -> None:
            self.saved_message_contents.append(
                [message.content for message in session.messages]
            )
            super().save(session)

    sessions = RecordingStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    class InspectingLLM:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            assert sessions.saved_message_contents == [["question"]]
            return LLMResponse("answer", LLMUsage(10, 3, 13), finish_reason="stop")

    llm = InspectingLLM()
    runtime = AgentRuntime(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    request = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("question"),
    )

    events = await _collect(await runtime.submit(request))

    session = sessions.load("session_001")
    assert [message.role for message in session.messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert [message.content for message in session.messages] == ["question", "answer"]
    assert sessions.saved_message_contents == [["question"], ["question", "answer"]]
    assert [event.event_type for event in events] == [
        RuntimeEventType.REQUEST_ACCEPTED,
        RuntimeEventType.REQUEST_QUEUED,
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.ASSISTANT_MESSAGE,
        RuntimeEventType.RUN_COMPLETED,
    ]
    assert all(event.session_id == "session_001" for event in events)
    assert all(event.invocation_id == "request_001" for event in events)
    assert all(event.sequence_number == 1 for event in events)
    assert all(event.source is InvocationSource.CLI for event in events)
    assert events[-2].event_type is RuntimeEventType.ASSISTANT_MESSAGE
    assert events[-2].payload["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "total_tokens": 13,
    }
    nested_usage = cast(dict[str, object], events[-2].payload["usage"])
    with pytest.raises(TypeError):
        nested_usage["prompt_tokens"] = 99
    assert events[-1].payload["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "total_tokens": 13,
    }
    assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED


def test_runtime_maps_expected_llm_error_to_failed_event() -> None:
    asyncio.run(_assert_runtime_maps_expected_llm_error_to_failed_event())


async def _assert_runtime_maps_expected_llm_error_to_failed_event() -> None:
    class FailingLLM:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise LLMError("provider temporarily unavailable")

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    runtime = AgentRuntime(
        llm=FailingLLM(),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    request = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("question"),
    )

    events = await _collect(await runtime.submit(request))

    assert events[-1].event_type is RuntimeEventType.RUN_FAILED
    assert events[-1].payload == {
        "error_type": "LLMError",
        "message": "provider temporarily unavailable",
    }
    assert [message.content for message in sessions.load("session_001").messages] == [
        "question"
    ]


def test_runtime_automatically_compacts_before_normal_completion() -> None:
    asyncio.run(_assert_runtime_automatically_compacts_before_normal_completion())


async def _assert_runtime_automatically_compacts_before_normal_completion() -> None:
    class CharacterTokenCounter:
        def count(self, request: LLMRequest) -> int:
            return sum(len(message.content) for message in request.messages)

    messages = tuple(
        Message(
            message_id=f"historical_{sequence:03d}",
            role=MessageRole.USER if sequence % 2 else MessageRole.ASSISTANT,
            content=f"historical-{sequence:02d}",
            created_at=NOW,
            sequence_number=sequence,
        )
        for sequence in range(1, 11)
    )
    sessions = InMemorySessionStore()
    sessions.create(
        Session(
            "session_001",
            "test",
            messages,
            "",
            NOW,
            NOW,
            next_sequence_number=11,
        )
    )
    llm = FakeLLMClient(
        [
            LLMResponse("merged history", LLMUsage(10, 3, 13), "stop"),
            LLMResponse("current answer", LLMUsage(20, 4, 24), "stop"),
        ]
    )
    coordinator = SessionCoordinator()
    context_builder = ContextBuilder("system")
    compaction = CompactionService(
        llm=llm,
        sessions=sessions,
        context_builder=context_builder,
        token_counter=CharacterTokenCounter(),
        coordinator=coordinator,
        clock=FakeClock(NOW),
        compact_prompt="summarize old turns",
        budget=300,
        trigger_ratio=0.5,
        target_ratio=0.25,
        keep_recent_turns=4,
        output_reserve=10,
    )
    runtime = AgentRuntime(
        llm=llm,
        sessions=sessions,
        context_builder=context_builder,
        coordinator=coordinator,
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        compaction=compaction,
    )
    request = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("current question"),
    )

    events = await _collect(await runtime.submit(request))

    stored = sessions.load("session_001")
    assert stored.summary == "merged history"
    assert [message.content for message in stored.messages] == [
        *[f"historical-{sequence:02d}" for sequence in range(3, 11)],
        "current question",
        "current answer",
    ]
    assert [event.event_type for event in events] == [
        RuntimeEventType.REQUEST_ACCEPTED,
        RuntimeEventType.REQUEST_QUEUED,
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.COMPACTION_COMPLETED,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.ASSISTANT_MESSAGE,
        RuntimeEventType.RUN_COMPLETED,
    ]
    assert len(llm.requests) == 2
    compaction_event = events[4]
    assert compaction_event.payload["summary_prompt_tokens"] == 10
    assert compaction_event.payload["token_counter_mode"] == "deterministic"
    assert llm.requests[0].messages[0].content == "summarize old turns"
    assert any(
        message.message_id == "session_summary"
        and message.content == "Session summary:\nmerged history"
        for message in llm.requests[1].messages
    )


def test_immediate_cancel_always_emits_terminal_event() -> None:
    asyncio.run(_assert_immediate_cancel_always_emits_terminal_event())


async def _assert_immediate_cancel_always_emits_terminal_event() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    llm = FakeLLMClient([LLMResponse("unused", LLMUsage(1, 1, 2), "stop")])
    runtime = AgentRuntime(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    request = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("question"),
    )

    handle = await runtime.submit(request)
    handle.cancel()
    events = await asyncio.wait_for(_collect(handle), timeout=0.5)

    assert events[-1].event_type is RuntimeEventType.RUN_CANCELLED
    assert llm.requests == []
    assert sessions.load("session_001").messages == ()
    await runtime.aclose()


def test_cancel_during_llm_keeps_user_and_emits_one_terminal_event() -> None:
    asyncio.run(_assert_cancel_during_llm_keeps_user_and_emits_one_terminal_event())


async def _assert_cancel_during_llm_keeps_user_and_emits_one_terminal_event() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    llm = FakeLLMClient(
        [LLMResponse("unused", LLMUsage(1, 1, 2), "stop")],
        delay_seconds=10,
    )
    runtime = AgentRuntime(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    request = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("question"),
    )

    handle = await runtime.submit(request)
    while not llm.requests:
        await asyncio.sleep(0)
    handle.cancel()
    events = await asyncio.wait_for(_collect(handle), timeout=0.5)

    terminal_events = [
        event for event in events if event.event_type in {RuntimeEventType.RUN_CANCELLED}
    ]
    assert len(terminal_events) == 1
    assert [message.content for message in sessions.load("session_001").messages] == [
        "question"
    ]
    await runtime.aclose()


async def _collect(handle: RunHandle) -> list[RuntimeEvent]:
    collected: list[RuntimeEvent] = []
    async for event in handle.events():
        collected.append(event)
    return collected
