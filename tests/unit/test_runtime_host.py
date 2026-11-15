from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sjtuclaw.errors import AttachmentError
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.infrastructure.persistence.attachments import JsonAttachmentStore
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMStreamEvent, LLMUsage
from sjtuclaw.models.message import UserInputKind
from sjtuclaw.models.runtime import (
    InvocationSource,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRequest,
    UserInput,
)
from sjtuclaw.models.session import (
    PendingRequest,
    RunState,
    RunStatus,
    Session,
    SessionArchiveReason,
)
from sjtuclaw.runtime import ContextBuilder, RuntimeHost, SessionCoordinator
from sjtuclaw.runtime.session_title import SessionTitleResult
from tests.fakes import FakeClock, FakeIdGenerator, FakeLLMClient

NOW = datetime(2026, 7, 19, tzinfo=UTC)


class RecordingSessionTitleGenerator:
    def __init__(self, result: SessionTitleResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def generate(
        self,
        user_text: str,
        assistant_text: str,
    ) -> SessionTitleResult:
        self.calls.append((user_text, assistant_text))
        return self.result


class FailingSessionTitleGenerator:
    async def generate(
        self,
        user_text: str,
        assistant_text: str,
    ) -> SessionTitleResult:
        raise RuntimeError("title provider failed")


def test_runtime_host_persists_then_atomically_starts_and_completes() -> None:
    asyncio.run(_assert_runtime_host_persists_then_atomically_starts_and_completes())


async def _assert_runtime_host_persists_then_atomically_starts_and_completes() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    clock = FakeClock(NOW, elapsed_ms=10)
    host = RuntimeHost(
        llm=FakeLLMClient([LLMResponse("答复", LLMUsage(1, 1, 2), "stop")]),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=clock,
        ids=FakeIdGenerator(),
    )
    request = RuntimeRequest(
        "request_001", "session_001", InvocationSource.GATEWAY, UserInput("问题")
    )

    handle = await host.submit(request)
    events = [event async for event in handle.events()]
    stored = sessions.load("session_001")

    assert stored.pending_requests == ()
    assert [message.content for message in stored.messages] == ["问题", "答复"]
    assert stored.runs[0].status is RunStatus.COMPLETED
    assert stored.runs[0].assistant_message_id == stored.messages[-1].message_id
    assert stored.title == "测试"
    assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED
    assert events[-1].payload["assistant_message_id"] == stored.messages[-1].message_id
    await host.aclose()


def test_runtime_host_names_only_the_first_completed_gateway_conversation() -> None:
    asyncio.run(_assert_runtime_host_names_only_first_gateway_conversation())


async def _assert_runtime_host_names_only_first_gateway_conversation() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "新会话", (), "", NOW, NOW))
    title_generator = RecordingSessionTitleGenerator(
        SessionTitleResult("课程复习重点整理", LLMUsage(3, 2, 5))
    )
    host = RuntimeHost(
        llm=FakeLLMClient(
            [
                LLMResponse("第一轮答复", LLMUsage(1, 1, 2), "stop"),
                LLMResponse("第二轮答复", LLMUsage(1, 1, 2), "stop"),
            ]
        ),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        session_titles=title_generator,
    )

    first = await host.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("  帮我   整理课程复习重点  "),
        )
    )
    await _drain(first)
    first_session = sessions.load("session_001")
    assert first_session.title == "课程复习重点整理"
    assert first_session.prompt_tokens == 4
    assert first_session.completion_tokens == 3
    assert title_generator.calls == [
        ("  帮我   整理课程复习重点  ", "第一轮答复")
    ]

    second = await host.submit(
        RuntimeRequest(
            "request_002",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("这轮不应该覆盖标题"),
        )
    )
    await _drain(second)
    assert sessions.load("session_001").title == "课程复习重点整理"
    assert title_generator.calls == [
        ("  帮我   整理课程复习重点  ", "第一轮答复")
    ]
    await host.aclose()


def test_runtime_host_does_not_generate_title_for_manually_named_session() -> None:
    asyncio.run(_assert_runtime_host_does_not_generate_title_for_manually_named_session())


async def _assert_runtime_host_does_not_generate_title_for_manually_named_session() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "手动标题", (), "", NOW, NOW))
    title_generator = RecordingSessionTitleGenerator(
        SessionTitleResult("不应使用", LLMUsage(3, 2, 5))
    )
    host = RuntimeHost(
        llm=FakeLLMClient(
            [LLMResponse("第一轮答复", LLMUsage(1, 1, 2), "stop")]
        ),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        session_titles=title_generator,
    )

    handle = await host.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("第一轮问题"),
        )
    )
    await _drain(handle)

    assert sessions.load("session_001").title == "手动标题"
    assert title_generator.calls == []
    await host.aclose()


def test_runtime_host_title_failure_falls_back_without_failing_run() -> None:
    asyncio.run(_assert_runtime_host_title_failure_falls_back_without_failing_run())


async def _assert_runtime_host_title_failure_falls_back_without_failing_run() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "新会话", (), "", NOW, NOW))
    host = RuntimeHost(
        llm=FakeLLMClient(
            [LLMResponse("第一轮答复", LLMUsage(1, 1, 2), "stop")]
        ),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        session_titles=FailingSessionTitleGenerator(),  # type: ignore[arg-type]
    )

    handle = await host.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("  需要   总结首轮问题  "),
        )
    )
    events = [event async for event in handle.events()]
    stored = sessions.load("session_001")

    assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED
    assert stored.runs[0].status is RunStatus.COMPLETED
    assert stored.title == "需要 总结首轮问题"
    await host.aclose()


def test_runtime_host_persists_attachment_refs_on_the_user_message() -> None:
    asyncio.run(_assert_runtime_host_persists_attachment_refs())


async def _assert_runtime_host_persists_attachment_refs() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    host = RuntimeHost(
        llm=FakeLLMClient([LLMResponse("答复", LLMUsage(1, 1, 2), "stop")]),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )

    handle = await host.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.CLI,
            UserInput("问题"),
            attachment_refs=("attachment_001",),
        )
    )
    await _drain(handle)

    assert sessions.load("session_001").messages[0].attachment_refs == (
        "attachment_001",
    )
    await host.aclose()


def test_runtime_host_rejects_attachment_from_another_session(tmp_path: Path) -> None:
    asyncio.run(_assert_runtime_host_rejects_cross_session_attachment(tmp_path))


async def _assert_runtime_host_rejects_cross_session_attachment(tmp_path: Path) -> None:
    attachments = JsonAttachmentStore(tmp_path)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"safe text"

    await attachments.ingest_text(
        session_id="session_001",
        attachment_id="attachment_001",
        display_name="notes.txt",
        chunks=chunks(),
        created_at=NOW,
    )
    sessions = InMemorySessionStore()
    sessions.create(Session("session_002", "测试", (), "", NOW, NOW))
    host = RuntimeHost(
        llm=FakeLLMClient([]),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        attachments=attachments,
    )

    with pytest.raises(AttachmentError, match="ATTACHMENT_SESSION_MISMATCH"):
        await host.submit(
            RuntimeRequest(
                "request_001",
                "session_002",
                InvocationSource.CLI,
                UserInput("问题"),
                attachment_refs=("attachment_001",),
            )
        )

    stored = sessions.load("session_002")
    assert stored.pending_requests == ()
    assert stored.runs == ()
    await host.aclose()


def test_runtime_host_applies_steer_at_a_streaming_safe_point() -> None:
    asyncio.run(_assert_runtime_host_applies_steer_at_a_streaming_safe_point())


async def _assert_runtime_host_applies_steer_at_a_streaming_safe_point() -> None:
    class StreamingLLM:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []
            self.never = asyncio.Event()

        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("streaming client must not use complete")

        async def _first_stream(self) -> object:
            yield LLMStreamEvent(content_delta="discarded partial")
            await self.never.wait()

        async def _guided_stream(self) -> object:
            yield LLMStreamEvent(
                response=LLMResponse("guided answer", LLMUsage(1, 1, 2), "stop")
            )

        def stream(self, request: LLMRequest) -> object:
            self.requests.append(request)
            return self._first_stream() if len(self.requests) == 1 else self._guided_stream()

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    llm = StreamingLLM()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    handle = await host.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.CLI,
            UserInput("原始问题"),
        )
    )
    while not llm.requests:
        await asyncio.sleep(0)
    run_id = sessions.load("session_001").runs[0].run_id

    result = await host.steer(
        "session_001",
        run_id,
        steer_id="steer_001",
        idempotency_key="steer_key_001",
        text="换一个方向",
    )
    events = [event async for event in handle.events()]
    stored = sessions.load("session_001")

    assert result == "STEER_ACCEPTED"
    assert [message.input_kind for message in stored.messages] == [
        UserInputKind.USER,
        UserInputKind.STEER,
        UserInputKind.USER,
    ]
    assert [message.content for message in stored.messages] == [
        "原始问题",
        "换一个方向",
        "guided answer",
    ]
    assert all("discarded partial" not in message.content for message in stored.messages)
    assert [event.event_type for event in events if event.event_type.name.startswith("STEER_")] == [
        RuntimeEventType.STEER_ACCEPTED,
        RuntimeEventType.STEER_WAITING_SAFE_POINT,
        RuntimeEventType.STEER_APPLIED,
    ]
    await host.aclose()


def test_runtime_host_rejects_submit_for_archived_session() -> None:
    async def scenario() -> None:
        sessions = InMemorySessionStore()
        sessions.create(
            Session(
                "session_001",
                "archived",
                (),
                "",
                NOW,
                NOW,
                archived_at=NOW,
                archived_reason=SessionArchiveReason.USER_ARCHIVED,
            )
        )
        host = RuntimeHost(
            llm=FakeLLMClient([LLMResponse("unused", LLMUsage(1, 1, 2), "stop")]),
            sessions=sessions,
            context_builder=ContextBuilder("system"),
            coordinator=SessionCoordinator(),
            clock=FakeClock(NOW),
            ids=FakeIdGenerator(),
        )

        with pytest.raises(Exception, match="SESSION_ARCHIVED"):
            await host.submit(
                RuntimeRequest(
                    "request_001",
                    "session_001",
                    InvocationSource.GATEWAY,
                    UserInput("must not run"),
                )
            )

        assert sessions.load("session_001").pending_requests == ()
        assert sessions.load("session_001").runs == ()
        await host.aclose()

    asyncio.run(scenario())


def test_runtime_host_atomically_converts_queued_request_to_guidance() -> None:
    asyncio.run(_assert_runtime_host_atomically_converts_queued_request_to_guidance())


async def _assert_runtime_host_atomically_converts_queued_request_to_guidance() -> None:
    class StreamingLLM:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []
            self.never = asyncio.Event()
            self.guided_release = asyncio.Event()

        async def complete(self, _request: LLMRequest) -> LLMResponse:
            raise AssertionError("streaming client must not use complete")

        async def _first_stream(self) -> object:
            yield LLMStreamEvent(content_delta="discarded partial")
            await self.never.wait()

        async def _guided_stream(self) -> object:
            await self.guided_release.wait()
            yield LLMStreamEvent(
                response=LLMResponse("guided answer", LLMUsage(1, 1, 2), "stop")
            )

        def stream(self, request: LLMRequest) -> object:
            self.requests.append(request)
            return self._first_stream() if len(self.requests) == 1 else self._guided_stream()

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    llm = StreamingLLM()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        max_concurrent_runs=1,
    )
    active = await host.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("first"),
        )
    )
    while not llm.requests:
        await asyncio.sleep(0)
    queued = await host.submit(
        RuntimeRequest(
            "request_002",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("turn this into guidance"),
        )
    )
    active_run = next(
        run
        for run in sessions.load("session_001").runs
        if run.status is RunStatus.STARTED
    )

    result = await host.steer_queued(
        "session_001",
        active_run.run_id,
        request_id="request_002",
        steer_id="steer_001",
        idempotency_key="steer_key_001",
    )
    repeated = await host.steer_queued(
        "session_001",
        active_run.run_id,
        request_id="request_002",
        steer_id="steer_001",
        idempotency_key="steer_key_001",
    )
    llm.guided_release.set()
    await _drain(active)
    await _drain(queued)
    stored = sessions.load("session_001")

    assert result == "STEER_ACCEPTED"
    assert repeated == "STEER_ACCEPTED"
    assert stored.pending_requests == ()
    assert next(run for run in stored.runs if run.request_id == "request_002").status is (
        RunStatus.CANCELLED_BEFORE_START
    )
    assert [message.content for message in stored.messages] == [
        "first",
        "turn this into guidance",
        "guided answer",
    ]
    assert len(llm.requests) == 2
    await host.aclose()


def test_runtime_host_queued_guidance_failed_preconditions_make_no_change() -> None:
    asyncio.run(_assert_runtime_host_queued_guidance_failed_preconditions_make_no_change())


async def _assert_runtime_host_queued_guidance_failed_preconditions_make_no_change() -> None:
    class BlockingLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, _request: LLMRequest) -> LLMResponse:
            self.started.set()
            await self.release.wait()
            return LLMResponse("answer", LLMUsage(1, 1, 2), "stop")

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    llm = BlockingLLM()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        max_concurrent_runs=1,
    )
    active = await host.submit(
        RuntimeRequest("request_001", "session_001", InvocationSource.GATEWAY, UserInput("first"))
    )
    await llm.started.wait()
    queued = await host.submit(
        RuntimeRequest("request_002", "session_001", InvocationSource.GATEWAY, UserInput("second"))
    )
    before = sessions.load("session_001")
    active_run = next(run for run in before.runs if run.status is RunStatus.STARTED)

    assert await host.steer_queued(
        "session_001",
        "missing_run",
        request_id="request_002",
        steer_id="steer_missing_run",
        idempotency_key="key_missing_run",
    ) == "RUN_NOT_ACTIVE"
    assert sessions.load("session_001") == before
    assert await host.steer_queued(
        "session_001",
        active_run.run_id,
        request_id="missing_request",
        steer_id="steer_missing_request",
        idempotency_key="key_missing_request",
    ) == "QUEUED_REQUEST_NOT_FOUND"
    assert sessions.load("session_001") == before

    assert await host.cancel_queued("session_001", "request_002")
    llm.release.set()
    await _drain(active)
    await _drain(queued)
    await host.aclose()


def test_runtime_host_cancels_queued_request_before_agent_loop() -> None:
    asyncio.run(_assert_runtime_host_cancels_queued_request_before_agent_loop())


async def _assert_runtime_host_cancels_queued_request_before_agent_loop() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    host = RuntimeHost(
        llm=FakeLLMClient([LLMResponse("unused", LLMUsage(1, 1, 2), "stop")]),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        max_concurrent_runs=1,
    )
    request = RuntimeRequest(
        "request_001", "session_001", InvocationSource.CLI, UserInput("问题")
    )

    await host.submit(request)
    assert await host.cancel_queued("session_001", "request_001") is True
    stored = sessions.load("session_001")
    assert stored.pending_requests == ()
    assert stored.runs[0].status is RunStatus.CANCELLED_BEFORE_START
    await host.aclose()


def test_runtime_host_round_robins_ready_sessions_after_each_run() -> None:
    asyncio.run(_assert_runtime_host_round_robins_ready_sessions_after_each_run())


async def _assert_runtime_host_round_robins_ready_sessions_after_each_run() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_a", "A", (), "", NOW, NOW))
    sessions.create(Session("session_b", "B", (), "", NOW, NOW))
    llm = FakeLLMClient(
        [
            LLMResponse("a-first", LLMUsage(1, 1, 2), "stop"),
            LLMResponse("b-first", LLMUsage(1, 1, 2), "stop"),
            LLMResponse("a-second", LLMUsage(1, 1, 2), "stop"),
        ]
    )
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        max_concurrent_runs=1,
    )
    first_a = await host.submit(
        RuntimeRequest("request_a1", "session_a", InvocationSource.CLI, UserInput("a1"))
    )
    second_a = await host.submit(
        RuntimeRequest("request_a2", "session_a", InvocationSource.CLI, UserInput("a2"))
    )
    first_b = await host.submit(
        RuntimeRequest("request_b1", "session_b", InvocationSource.CLI, UserInput("b1"))
    )

    await asyncio.gather(
        *(asyncio.create_task(_drain(handle)) for handle in (first_a, second_a, first_b))
    )

    assert [request.messages[-1].content for request in llm.requests] == ["a1", "b1", "a2"]
    await host.aclose()


async def _drain(handle: object) -> None:
    async for _ in handle.events():  # type: ignore[attr-defined]
        pass


async def _collect(handle: object) -> list[RuntimeEvent]:
    events: list[RuntimeEvent] = []
    async for event in handle.events():  # type: ignore[attr-defined]
        events.append(event)
    return events


def test_runtime_host_records_interrupted_run_during_recovery() -> None:
    asyncio.run(_assert_runtime_host_records_interrupted_run_during_recovery())


async def _assert_runtime_host_records_interrupted_run_during_recovery() -> None:
    sessions = InMemorySessionStore()
    sessions.create(
        Session(
            "session_001",
            "测试",
            (),
            "",
            NOW,
            NOW,
            runs=(
                RunState(
                    "run_001",
                    "request_001",
                    RunStatus.STARTED,
                    started_at=NOW,
                ),
            ),
        )
    )
    host = RuntimeHost(
        llm=FakeLLMClient([]),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )

    await host.recover_pending()
    stored = sessions.load("session_001")
    assert stored.runs[0].status is RunStatus.INTERRUPTED
    assert stored.run_records[-1].code == "RUN_INTERRUPTED_GATEWAY_RESTART"
    await host.aclose()


def test_runtime_host_recovers_a_pending_request_once_after_repeated_recovery() -> None:
    asyncio.run(_assert_runtime_host_recovers_pending_once())


async def _assert_runtime_host_recovers_pending_once() -> None:
    class BlockingLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            self.started.set()
            await self.release.wait()
            return LLMResponse("恢复答复", LLMUsage(1, 1, 2), "stop")

    sessions = InMemorySessionStore()
    sessions.create(
        Session(
            "session_001",
            "测试",
            (),
            "",
            NOW,
            NOW,
            pending_requests=(
                PendingRequest(
                    "request_001",
                    1,
                    InvocationSource.GATEWAY,
                    None,
                    "恢复请求",
                    NOW,
                    idempotency_key="recover_001",
                ),
            ),
            runs=(
                RunState(
                    "run_001",
                    "request_001",
                    RunStatus.QUEUED,
                    idempotency_key="recover_001",
                ),
            ),
        )
    )
    llm = BlockingLLM()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )

    try:
        await host.recover_pending()
        await host.recover_pending()
        await llm.started.wait()
        retry = await host.submit(
            RuntimeRequest(
                "request_001",
                "session_001",
                InvocationSource.GATEWAY,
                UserInput("恢复请求"),
                idempotency_key="recover_001",
            )
        )
        llm.release.set()
        events = await asyncio.wait_for(_collect(retry), timeout=0.1)

        assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED
        assert len(llm.requests) == 1
    finally:
        llm.release.set()
        await host.aclose()


def test_runtime_host_uses_persisted_submission_time_after_recovery() -> None:
    asyncio.run(_assert_runtime_host_uses_persisted_submission_time())


async def _assert_runtime_host_uses_persisted_submission_time() -> None:
    class BlockingLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.started.set()
            await self.release.wait()
            return LLMResponse("恢复答复", LLMUsage(1, 1, 2), "stop")

    recovered_at = NOW + timedelta(seconds=5)
    sessions = InMemorySessionStore()
    sessions.create(
        Session(
            "session_001",
            "测试",
            (),
            "",
            NOW,
            NOW,
            pending_requests=(
                PendingRequest(
                    "request_001",
                    1,
                    InvocationSource.GATEWAY,
                    None,
                    "恢复请求",
                    NOW,
                    idempotency_key="recover_001",
                ),
            ),
            runs=(
                RunState(
                    "run_001",
                    "request_001",
                    RunStatus.QUEUED,
                    idempotency_key="recover_001",
                ),
            ),
        )
    )
    llm = BlockingLLM()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(recovered_at, elapsed_ms=10),
        ids=FakeIdGenerator(),
    )

    try:
        await host.recover_pending()
        await llm.started.wait()
        retry = await host.submit(
            RuntimeRequest(
                "request_001",
                "session_001",
                InvocationSource.GATEWAY,
                UserInput("恢复请求"),
                idempotency_key="recover_001",
            )
        )
        llm.release.set()
        await _drain(retry)

        stored = sessions.load("session_001")
        assert stored.runs[0].duration_ms == 5_000
        assert stored.run_records[-2].code == "RUN_RECOVERED_TIMING"
    finally:
        llm.release.set()
        await host.aclose()


def test_runtime_host_returns_existing_handle_for_idempotent_retry() -> None:
    asyncio.run(_assert_runtime_host_idempotent_retry())


async def _assert_runtime_host_idempotent_retry() -> None:
    class BlockingLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            self.started.set()
            await self.release.wait()
            return LLMResponse("答复", LLMUsage(1, 1, 2), "stop")

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    llm = BlockingLLM()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    first = await host.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("问题"),
            idempotency_key="retry_001",
        )
    )
    await llm.started.wait()
    duplicate = await host.submit(
        RuntimeRequest(
            "request_002",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("重复提交内容不应进入队列"),
            idempotency_key="retry_001",
        )
    )

    assert duplicate is first
    assert len(sessions.load("session_001").runs) == 1
    assert sessions.load("session_001").runs[0].idempotency_key == "retry_001"
    assert len(llm.requests) == 1

    llm.release.set()
    await _drain(first)
    assert sessions.load("session_001").runs[0].idempotency_key == "retry_001"
    terminal_retry = await host.submit(
        RuntimeRequest(
            "request_003",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("终态重试也不能再次调用模型"),
            idempotency_key="retry_001",
        )
    )
    terminal_events = await _collect(terminal_retry)
    assert terminal_retry is not first
    assert terminal_events[-1].event_type is RuntimeEventType.RUN_COMPLETED
    assert len(llm.requests) == 1
    await host.aclose()


def test_runtime_host_cancels_later_queue_item_while_session_run_is_active() -> None:
    asyncio.run(_assert_runtime_host_cancels_later_queue_item())


async def _assert_runtime_host_cancels_later_queue_item() -> None:
    class BlockingLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, request: LLMRequest) -> LLMResponse:
            if request.messages[-1].content == "first":
                self.started.set()
                await self.release.wait()
            return LLMResponse("答复", LLMUsage(1, 1, 2), "stop")

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    llm = BlockingLLM()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        max_concurrent_runs=1,
    )
    first = await host.submit(
        RuntimeRequest("request_001", "session_001", InvocationSource.CLI, UserInput("first"))
    )
    await llm.started.wait()
    await host.submit(
        RuntimeRequest("request_002", "session_001", InvocationSource.CLI, UserInput("second"))
    )

    assert await asyncio.wait_for(
        host.cancel_queued("session_001", "request_002"),
        timeout=0.2,
    )
    assert sessions.load("session_001").runs[1].status is RunStatus.CANCELLED_BEFORE_START

    llm.release.set()
    await _drain(first)
    assert [message.content for message in sessions.load("session_001").messages] == [
        "first",
        "答复",
    ]
    await host.aclose()


def test_runtime_host_keeps_pending_work_saved_during_active_run() -> None:
    asyncio.run(_assert_runtime_host_keeps_pending_work_saved_during_active_run())


async def _assert_runtime_host_keeps_pending_work_saved_during_active_run() -> None:
    class FirstRunBlocks:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, request: LLMRequest) -> LLMResponse:
            prompt = request.messages[-1].content
            if prompt == "first":
                self.started.set()
                await self.release.wait()
            return LLMResponse(f"{prompt} answer", LLMUsage(1, 1, 2), "stop")

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    llm = FirstRunBlocks()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        max_concurrent_runs=1,
    )
    first = await host.submit(
        RuntimeRequest("request_001", "session_001", InvocationSource.CLI, UserInput("first"))
    )
    await llm.started.wait()
    second = await host.submit(
        RuntimeRequest("request_002", "session_001", InvocationSource.CLI, UserInput("second"))
    )

    assert [item.request_id for item in sessions.load("session_001").pending_requests] == [
        "request_002"
    ]
    llm.release.set()
    await _drain(first)
    await asyncio.wait_for(_drain(second), timeout=0.2)
    stored = sessions.load("session_001")

    assert stored.pending_requests == ()
    assert [run.status for run in stored.runs] == [RunStatus.COMPLETED, RunStatus.COMPLETED]
    assert [message.content for message in stored.messages] == [
        "first",
        "first answer",
        "second",
        "second answer",
    ]
    await host.aclose()


def test_runtime_host_stops_active_run_and_starts_next_queue_item() -> None:
    asyncio.run(_assert_runtime_host_stop_and_continue())


async def _assert_runtime_host_stop_and_continue() -> None:
    class FirstRunBlocks:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, request: LLMRequest) -> LLMResponse:
            if request.messages[-1].content == "first":
                self.started.set()
                await asyncio.Event().wait()
            return LLMResponse("second answer", LLMUsage(1, 1, 2), "stop")

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    clock = FakeClock(NOW, elapsed_ms=10)
    llm = FirstRunBlocks()
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=clock,
        ids=FakeIdGenerator(),
        max_concurrent_runs=1,
    )
    first = await host.submit(
        RuntimeRequest("request_001", "session_001", InvocationSource.CLI, UserInput("first"))
    )
    await llm.started.wait()
    second = await host.submit(
        RuntimeRequest("request_002", "session_001", InvocationSource.CLI, UserInput("second"))
    )
    active_run = sessions.load("session_001").runs[0]
    clock.elapsed_ms = 35

    assert await host.stop_run("session_001", active_run.run_id)
    assert not await host.stop_run("session_001", active_run.run_id)
    first_events = await _collect(first)
    second_events = await _collect(second)
    stored = sessions.load("session_001")

    assert first_events[-1].event_type is RuntimeEventType.RUN_CANCELLED
    assert stored.runs[0].status is RunStatus.CANCELLED
    assert stored.runs[0].duration_ms == 25
    assert second_events[-1].event_type is RuntimeEventType.RUN_COMPLETED
    assert stored.runs[1].status is RunStatus.COMPLETED
    assert stored.messages[-1].content == "second answer"
    await host.aclose()
