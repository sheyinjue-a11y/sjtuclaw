"""Tool-aware AgentRuntime safety, persistence, and event behavior."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest

from sjtuclaw.config import ProviderConfiguration, ProviderKind
from sjtuclaw.errors import LLMError, StoreError
from sjtuclaw.infrastructure.llm.anthropic import AnthropicLLMClient
from sjtuclaw.infrastructure.llm.tool_protocol import json_messages, native_messages
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.infrastructure.tools.registry import ToolHandler, ToolRegistry
from sjtuclaw.infrastructure.tools.skill_selection import SelectSkillTool
from sjtuclaw.models.llm import LLMResponse, LLMStreamEvent, LLMToolMode, LLMUsage
from sjtuclaw.models.memory import MemoryCategory, MemoryEntry
from sjtuclaw.models.message import Message, MessageRole, UserInputKind
from sjtuclaw.models.runtime import (
    InvocationSource,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRequest,
    UserInput,
)
from sjtuclaw.models.session import ApprovalDecisionStatus, RunState, RunStatus, Session
from sjtuclaw.models.skill import SkillManifest, SkillReference, SkillUsageSource
from sjtuclaw.models.tool import (
    JsonValue,
    PermissionDecision,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionOutput,
    ToolResourceLink,
    ToolSafetyLevel,
    mutable_json,
)
from sjtuclaw.runtime import (
    AgentRuntime,
    ContextBuilder,
    PermissionPolicy,
    RuntimeHost,
    SessionCoordinator,
)
from sjtuclaw.runtime.agent_loop import AgentLoop
from sjtuclaw.runtime.memory_capture import MemoryCapture
from sjtuclaw.runtime.memory_extractor import (
    MemoryExtractionError,
    MemoryExtractor,
    MemoryOperation,
    MemoryOperationType,
    MemoryTurn,
)
from sjtuclaw.runtime.memory_policy import MemoryPolicyService, MemoryPolicySnapshot
from sjtuclaw.runtime.memory_retriever import LocalMemoryRetriever
from sjtuclaw.runtime.memory_service import MemoryService
from sjtuclaw.runtime.session_title import SessionTitleGenerator
from sjtuclaw.runtime.skill_service import SkillService
from tests.fakes import FakeClock, FakeIdGenerator, FakeLLMClient

NOW = datetime(2026, 7, 15, tzinfo=UTC)


@dataclass
class RecordingTool:
    executions: list[str]
    resources: tuple[ToolResourceLink, ...] = ()

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="record",
            description="Record a safe test value.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ("value",),
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ("value",),
                "additionalProperties": False,
            },
            result_usage="Use value as the recorded test output.",
            safety_level=ToolSafetyLevel.READ_ONLY,
            allowed_resource_kinds=("download",),
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        value = arguments["value"]
        assert isinstance(value, str)
        self.executions.append(value)
        rendered = f"observed:{value}"
        return ToolExecutionOutput(
            content=rendered,
            structured_content={"value": rendered},
            resources=self.resources,
        )


class DenyPolicy(PermissionPolicy):
    def decide(
        self,
        intent: object,
        context: ToolExecutionContext | None = None,
    ) -> PermissionDecision:
        del intent, context
        return PermissionDecision.DENY


class AskPolicy(PermissionPolicy):
    def decide(
        self,
        intent: object,
        context: ToolExecutionContext | None = None,
    ) -> PermissionDecision:
        del intent, context
        return PermissionDecision.ASK


@dataclass
class RecordingMemoryCapture:
    calls: list[tuple[Session, RuntimeRequest]]
    error: Exception | None = None

    async def capture(self, session: Session, request: RuntimeRequest) -> None:
        self.calls.append((session, request))
        if self.error is not None:
            raise self.error


def _runtime(
    responses: Sequence[LLMResponse],
    *,
    policy_type: type[PermissionPolicy] = PermissionPolicy,
    max_steps: int = 20,
    memory_capture: RecordingMemoryCapture | MemoryCapture | None = None,
    resources: tuple[ToolResourceLink, ...] = (),
    skills: SkillService | None = None,
) -> tuple[AgentRuntime, InMemorySessionStore, FakeLLMClient, RecordingTool]:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    tool = RecordingTool([], resources)
    registered_tools: tuple[ToolHandler, ...] = (tool,)
    if skills is not None:
        registered_tools = (*registered_tools, SelectSkillTool(skills=skills))
    registry = ToolRegistry(registered_tools)
    policy = policy_type(registry.definitions(), allowed_roots=(Path.cwd(),))
    llm = FakeLLMClient(list(responses))
    runtime = AgentRuntime(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        tools=registry,
        permission=policy,
        tool_mode=LLMToolMode.NATIVE,
        max_steps=max_steps,
        skills=skills,
        memory_capture=cast(MemoryCapture | None, memory_capture),
    )
    return runtime, sessions, llm, tool


def _request() -> RuntimeRequest:
    return RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("use the safe tool"),
    )


def _response(
    content: str,
    *,
    calls: tuple[ToolCall, ...] = (),
    reasoning: str | None = None,
) -> LLMResponse:
    return LLMResponse(
        content,
        LLMUsage(11, 3, 14, 7, 4),
        "tool_calls" if calls else "stop",
        tool_calls=calls,
        reasoning_content=reasoning,
    )


async def _events(runtime: AgentRuntime) -> list[RuntimeEvent]:
    handle = await runtime.submit(_request())
    events: list[RuntimeEvent] = []
    async for event in handle.events():
        events.append(event)
    return events


class _StaticSkills:
    def __init__(self, manifests: tuple[SkillManifest, ...]) -> None:
        self._manifests = manifests

    def list(self) -> tuple[SkillManifest, ...]:
        return self._manifests

    def resolve_current_names(
        self,
        skill_ids: tuple[str, ...],
    ) -> tuple[SkillReference, ...]:
        references: dict[str, SkillReference] = {}
        for manifest in self._manifests:
            snapshot_id = manifest.snapshot_id
            snapshot_hash = manifest.snapshot_hash
            version = manifest.version
            assert snapshot_id is not None
            assert snapshot_hash is not None
            assert version is not None
            references[manifest.skill_id] = SkillReference(
                manifest.skill_id,
                snapshot_id,
                snapshot_hash,
                version,
            )
        return tuple(references[skill_id] for skill_id in skill_ids)


def _skill_reference(skill_id: str) -> SkillReference:
    return SkillReference(skill_id, f"{skill_id}-{'a' * 16}", "b" * 64, "1.0.0")


def _skill_manifest(skill_id: str) -> SkillManifest:
    reference = _skill_reference(skill_id)
    return SkillManifest(
        skill_id,
        "test skill",
        reference.version,
        reference.snapshot_id,
        reference.snapshot_hash,
        True,
    )


def test_explicit_skill_emits_structured_selection_progress() -> None:
    asyncio.run(_assert_explicit_skill_emits_structured_selection_progress())


async def _assert_explicit_skill_emits_structured_selection_progress() -> None:
    runtime, _, _, _ = _runtime((_response("done"),))
    reference = SkillReference(
        "course-report",
        "course-report-1234567890abcdef",
        "a" * 64,
        "1.0.0",
    )
    request = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("write a report"),
        skill_refs=(reference,),
    )

    handle = await runtime.submit(request)
    events = [event async for event in handle.events()]
    selected = next(
        event
        for event in events
        if event.event_type is RuntimeEventType.RUN_PROGRESS
        and event.payload["code"] == "skill.selected"
    )

    assert selected.payload == {
        "code": "skill.selected",
        "skill_id": "course-report",
        "source": "explicit",
        "reason": "explicit local Skill reference",
    }
    await runtime.aclose()


def test_automatic_skill_usage_is_distinct_from_explicit_selection() -> None:
    asyncio.run(_assert_automatic_skill_usage_is_distinct_from_explicit_selection())


async def _assert_automatic_skill_usage_is_distinct_from_explicit_selection() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    explicit = _skill_reference("course-report")
    runtime = RuntimeHost(
        llm=FakeLLMClient([_response("done")]),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        skills=cast(SkillService, _StaticSkills((_skill_manifest("presentations"),))),
    )
    try:
        handle = await runtime.submit(
            RuntimeRequest(
                "request_001",
                "session_001",
                InvocationSource.CLI,
                UserInput("create a PPT report"),
                skill_refs=(explicit,),
            )
        )
        events = [event async for event in handle.events()]
        stored = sessions.load("session_001")

        assert tuple(reference.skill_id for reference in stored.runs[0].selected_skill_refs) == (
            "course-report",
            "presentations",
        )
        assert stored.explicit_skill_refs == (explicit,)
        assert [(usage.reference.skill_id, usage.source) for usage in stored.skill_usages] == [
            ("course-report", SkillUsageSource.EXPLICIT),
            ("presentations", SkillUsageSource.AUTO),
        ]
        selected = [
            event.payload
            for event in events
            if event.event_type is RuntimeEventType.RUN_PROGRESS
            and event.payload["code"] == "skill.selected"
        ]
        assert selected == [
            {
                "code": "skill.selected",
                "skill_id": "course-report",
                "source": "explicit",
                "reason": "explicit local Skill reference",
            },
            {
                "code": "skill.selected",
                "skill_id": "presentations",
                "source": "auto",
                "reason": "automatic presentations routing",
            },
        ]
    finally:
        await runtime.aclose()


def test_native_tool_loop_persists_structured_trace_and_event_order() -> None:
    asyncio.run(_assert_native_tool_loop_persists_structured_trace_and_event_order())


async def _assert_native_tool_loop_persists_structured_trace_and_event_order() -> None:
    call = ToolCall("call_001", "record", '{"value":"first"}')
    resource = ToolResourceLink(
        "download",
        "report.md",
        "/downloads/download_123",
        "text/markdown",
        "2026-07-31T00:00:00+00:00",
    )
    runtime, sessions, llm, tool = _runtime(
        (_response("", calls=(call,), reasoning="must remain provider-only"), _response("done")),
        resources=(resource,),
    )

    events = await _events(runtime)
    stored = sessions.load("session_001")

    assert tool.executions == ["first"]
    assert [message.role for message in stored.messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert stored.messages[1].tool_calls == (call,)
    assert stored.messages[1].reasoning_content == "must remain provider-only"
    assert stored.messages[2].tool_call_id == "call_001"
    assert stored.messages[2].tool_name == "record"
    assert stored.messages[2].tool_structured_content is not None
    assert mutable_json(stored.messages[2].tool_structured_content) == {
        "value": "observed:first"
    }
    assert stored.messages[2].tool_resources == (resource,)
    assert stored.run_records[-1].code == "RUN_COMPLETED"
    assert stored.prompt_tokens == 22
    assert stored.prompt_cache_hit_tokens == 14
    assert stored.prompt_cache_miss_tokens == 8
    assert stored.completion_tokens == 6
    assert stored.cache_breakdown_complete is True
    assert [event.event_type for event in events] == [
        RuntimeEventType.REQUEST_ACCEPTED,
        RuntimeEventType.REQUEST_QUEUED,
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.TOOL_CALL,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.TOOL_RESULT,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.RUN_PROGRESS,
        RuntimeEventType.ASSISTANT_MESSAGE,
        RuntimeEventType.RUN_COMPLETED,
    ]
    assert events[5].payload == {
        "call_id": "call_001",
        "name": "record",
        "arguments": '{"value":"first"}',
    }
    assert events[7].payload["status"] == "success"
    assert events[7].payload["structured_content"] == {"value": "observed:first"}
    assert mutable_json(cast(JsonValue, events[7].payload["resources"])) == [
        {
            "kind": "download",
            "name": "report.md",
            "href": "/downloads/download_123",
            "mime_type": "text/markdown",
            "expires_at": "2026-07-31T00:00:00+00:00",
        }
    ]
    assert llm.requests[0].tool_mode is LLMToolMode.NATIVE
    assert [definition.name for definition in llm.requests[0].tools] == ["record"]
    assert llm.requests[1].messages[-2].tool_calls == (call,)

    second_request = llm.requests[1]
    native_payload = native_messages(second_request.messages, second_request.tools)
    native_tool_message = next(
        item for item in native_payload if item.get("role") == "tool"
    )
    native_content = native_tool_message["content"]
    assert isinstance(native_content, str)
    native_result = json.loads(native_content)

    json_payload = json_messages(second_request.messages, second_request.tools)
    json_tool_message = next(
        item
        for item in json_payload
        if isinstance(item.get("content"), str)
        and '"type":"tool_results"' in cast(str, item["content"])
    )
    json_envelope = json.loads(cast(str, json_tool_message["content"]))
    assert isinstance(json_envelope, dict)
    json_results = json_envelope["results"]
    assert isinstance(json_results, list)
    assert len(json_results) == 1
    json_result_item = json_results[0]
    assert isinstance(json_result_item, dict)
    json_result_content = json_result_item["content"]
    assert isinstance(json_result_content, str)
    json_result = json.loads(json_result_content)

    anthropic_requests: list[httpx.Request] = []

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        anthropic_requests.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    anthropic = AnthropicLLMClient(
        ProviderConfiguration(
            provider=ProviderKind.ANTHROPIC,
            model="test-model",
            base_url="https://anthropic.example",
            api_key="test-key",
        ),
        transport=httpx.MockTransport(anthropic_handler),
    )
    try:
        await anthropic.complete(second_request)
    finally:
        await anthropic.aclose()
    anthropic_payload = json.loads(anthropic_requests[0].content)
    assert isinstance(anthropic_payload, dict)
    anthropic_messages = anthropic_payload["messages"]
    assert isinstance(anthropic_messages, list)
    anthropic_tool_message = anthropic_messages[-1]
    assert isinstance(anthropic_tool_message, dict)
    anthropic_blocks = anthropic_tool_message["content"]
    assert isinstance(anthropic_blocks, list)
    assert len(anthropic_blocks) == 1
    anthropic_block = anthropic_blocks[0]
    assert isinstance(anthropic_block, dict)
    anthropic_content = anthropic_block["content"]
    assert isinstance(anthropic_content, str)
    anthropic_result = json.loads(anthropic_content)

    expected_model_result = {
        "content": "observed:first",
        "structured_content": {"value": "observed:first"},
    }
    assert native_result == expected_model_result
    assert json_result == expected_model_result
    assert anthropic_result == expected_model_result
    assert json_envelope == {
        "type": "tool_results",
        "results": [
            {
                "call_id": "call_001",
                "tool": "record",
                "status": "success",
                "content": json_result_content,
                "error_code": None,
            }
        ],
    }

    provider_payloads = (native_payload, json_payload, anthropic_payload)
    for payload in provider_payloads:
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "download_123" not in serialized
        assert '"href"' not in serialized
        assert '"resources"' not in serialized


def test_skill_selection_successes_persist_the_same_structured_reference() -> None:
    asyncio.run(_assert_skill_selection_successes_persist_the_same_structured_reference())


async def _assert_skill_selection_successes_persist_the_same_structured_reference() -> None:
    call = ToolCall(
        "call_001",
        "select_skill",
        '{"skill_id":"course-report","reason":"needed for the report"}',
    )
    repeated = ToolCall(
        "call_002",
        "select_skill",
        '{"skill_id":"course-report","reason":"still needed"}',
    )
    skills = cast(SkillService, _StaticSkills((_skill_manifest("course-report"),)))
    runtime, sessions, _, _ = _runtime(
        (
            _response("", calls=(call,)),
            _response("", calls=(repeated,)),
            _response("done"),
        ),
        skills=skills,
    )

    await _events(runtime)
    stored = sessions.load("session_001")
    results = tuple(
        message for message in stored.messages if message.role is MessageRole.TOOL
    )
    expected = {
        "skill_id": "course-report",
        "snapshot_id": f"course-report-{'a' * 16}",
        "version": "1.0.0",
    }

    assert len(results) == 2
    assert [
        mutable_json(message.tool_structured_content)
        for message in results
        if message.tool_structured_content is not None
    ] == [expected, expected]
    assert all(message.tool_resources == () for message in results)


def test_invalid_batch_persists_all_errors_without_executing_tools() -> None:
    asyncio.run(_assert_invalid_batch_persists_all_errors_without_executing_tools())


async def _assert_invalid_batch_persists_all_errors_without_executing_tools() -> None:
    calls = (
        ToolCall("call_001", "record", '{"value":"would-run"}'),
        ToolCall("call_002", "record", "{malformed"),
    )
    runtime, sessions, _, tool = _runtime((_response("", calls=calls), _response("fixed")))

    events = await _events(runtime)
    stored = sessions.load("session_001")
    results = [message for message in stored.messages if message.role is MessageRole.TOOL]

    assert tool.executions == []
    assert [message.tool_error_code for message in results] == [
        "BATCH_REJECTED",
        "INVALID_ARGUMENTS_JSON",
    ]
    assert [event.event_type for event in events].count(RuntimeEventType.TOOL_RESULT) == 2
    assert {record.code for record in stored.run_records} >= {
        "BATCH_REJECTED",
        "INVALID_ARGUMENTS_JSON",
    }


def test_denied_tool_calls_return_structured_observation() -> None:
    asyncio.run(_assert_denied_tool_call_returns_structured_observation())


async def _assert_denied_tool_call_returns_structured_observation() -> None:
    call = ToolCall("call_001", "record", '{"value":"blocked"}')
    runtime, sessions, _, tool = _runtime(
        (_response("", calls=(call,)), _response("handled")),
        policy_type=DenyPolicy,
    )

    events = await _events(runtime)
    stored = sessions.load("session_001")
    tool_message = next(
        message for message in stored.messages if message.role is MessageRole.TOOL
    )

    assert tool.executions == []
    assert tool_message.tool_error_code == "PERMISSION_DENIED"
    assert any(record.code == "PERMISSION_DENY" for record in stored.run_records)
    result_event = next(
        event for event in events if event.event_type is RuntimeEventType.TOOL_RESULT
    )
    assert result_event.payload["error_code"] == "PERMISSION_DENIED"


def test_runtime_host_resumes_a_persisted_approval() -> None:
    asyncio.run(_assert_runtime_host_resumes_a_persisted_approval())


async def _assert_runtime_host_resumes_a_persisted_approval() -> None:
    call = ToolCall("call_001", "record", '{"value":"approved"}')
    automatic = _skill_reference("presentations")
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "新会话", (), "", NOW, NOW))
    tool = RecordingTool([])
    registry = ToolRegistry((tool,))
    llm = FakeLLMClient(
        [
            _response("", calls=(call,)),
            _response("handled"),
            _response("审批工具执行"),
        ]
    )
    clock = FakeClock(NOW)
    capture = RecordingMemoryCapture([])
    host = RuntimeHost(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=clock,
        ids=FakeIdGenerator(),
        tools=registry,
        permission=AskPolicy(registry.definitions(), allowed_roots=(Path.cwd(),)),
        tool_mode=LLMToolMode.NATIVE,
        session_titles=SessionTitleGenerator(llm=llm, clock=clock),
        memory_capture=cast(MemoryCapture, capture),
    )
    handle = await host.submit(
        RuntimeRequest(
            "request_001",
            "session_001",
            InvocationSource.GATEWAY,
            UserInput("use the safe tool"),
            skill_refs=(automatic,),
            automatic_skill_ids=("presentations",),
        )
    )
    for _ in range(50):
        pending = sessions.load("session_001").pending_approval
        if pending is not None:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("approval was not persisted")

    checkpoint = sessions.load("session_001").runs[0].checkpoint
    assert checkpoint is not None
    assert checkpoint.automatic_skill_ids == (
        "presentations",
    )

    await host.decide_approval(
        "session_001",
        pending.approval_id,
        status=ApprovalDecisionStatus.APPROVED,
        idempotency_key="approve_001",
        actor_id="cli_user",
    )
    events = [event async for event in handle.events()]
    stored = sessions.load("session_001")

    assert tool.executions == ["approved"]
    assert stored.pending_approval is None
    assert stored.runs[0].status.value == "completed"
    assert stored.title == "审批工具执行"
    assert len(capture.calls) == 1
    assert any(event.event_type is RuntimeEventType.APPROVAL_RESUMED for event in events)
    await host.aclose()


def test_sixth_consecutive_signature_is_blocked_without_execution() -> None:
    asyncio.run(_assert_sixth_consecutive_signature_is_blocked_without_execution())


async def _assert_sixth_consecutive_signature_is_blocked_without_execution() -> None:
    call = ToolCall("call_001", "record", '{"value":"repeat"}')
    responses = tuple(_response("", calls=(call,)) for _ in range(6)) + (_response("done"),)
    runtime, sessions, _, tool = _runtime(responses, max_steps=0)

    await _events(runtime)
    stored = sessions.load("session_001")
    results = [message for message in stored.messages if message.role is MessageRole.TOOL]

    assert tool.executions == ["repeat"] * 5
    assert len(results) == 6
    assert results[-1].tool_error_code == "REPEATED_TOOL_CALL"
    assert any(record.code == "REPEATED_TOOL_CALL" for record in stored.run_records)


def test_max_steps_stops_after_persisting_tool_result() -> None:
    asyncio.run(_assert_max_steps_stops_after_persisting_tool_result())


async def _assert_max_steps_stops_after_persisting_tool_result() -> None:
    call = ToolCall("call_001", "record", '{"value":"last"}')
    runtime, sessions, llm, tool = _runtime((_response("", calls=(call,)),), max_steps=1)

    events = await _events(runtime)
    stored = sessions.load("session_001")

    assert tool.executions == ["last"]
    assert len(llm.requests) == 1
    assert stored.messages[-1].role is MessageRole.TOOL
    assert any(record.code == "MAX_STEPS_REACHED" for record in stored.run_records)
    assert events[-1].event_type is RuntimeEventType.RUN_FAILED
    assert events[-1].payload["error_type"] == "AgentLimitError"


def test_cancellation_keeps_persisted_user_message_and_audit_record() -> None:
    asyncio.run(_assert_cancellation_keeps_persisted_user_message_and_audit_record())


async def _assert_cancellation_keeps_persisted_user_message_and_audit_record() -> None:
    runtime, sessions, llm, _ = _runtime((_response("unused"),))
    llm.delay_seconds = 10

    handle = await runtime.submit(_request())
    while not llm.requests:
        await asyncio.sleep(0)
    handle.cancel()
    events: list[RuntimeEvent] = []
    async for event in handle.events():
        events.append(event)

    stored = sessions.load("session_001")
    assert [message.role for message in stored.messages] == [MessageRole.USER]
    assert any(record.code == "RUN_CANCELLED" for record in stored.run_records)
    assert events[-1].event_type is RuntimeEventType.RUN_CANCELLED


def test_streaming_emits_deltas_but_persists_only_completed_assistant_message() -> None:
    asyncio.run(_assert_streaming_persistence_boundary())


async def _assert_streaming_persistence_boundary() -> None:
    class StreamingLLM:
        def __init__(self) -> None:
            self.requests: list[object] = []

        async def complete(self, request: object) -> LLMResponse:
            raise AssertionError("streaming client must not use complete")

        async def _events(self) -> object:
            yield LLMStreamEvent(content_delta="第一段")
            yield LLMStreamEvent(content_delta="，第二段")
            yield LLMStreamEvent(
                response=LLMResponse(
                    "第一段，第二段",
                    LLMUsage(1, 2, 3),
                    "stop",
                    reasoning_content="仅用于回放",
                )
            )

        def stream(self, request: object) -> object:
            self.requests.append(request)
            return self._events()

    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    llm = StreamingLLM()
    runtime = AgentRuntime(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    handle = await runtime.submit(_request())
    events = [event async for event in handle.events()]
    stored = sessions.load("session_001")

    deltas = [event.payload["delta"] for event in events if event.event_type is RuntimeEventType.ASSISTANT_DELTA]
    assert deltas == ["第一段", "，第二段"]
    assert stored.messages[-1].content == "第一段，第二段"
    assert stored.messages[-1].reasoning_content == "仅用于回放"
    assert all("仅用于回放" not in str(event.payload) for event in events)
    assert [
        event.payload["code"]
        for event in events
        if event.event_type is RuntimeEventType.RUN_PROGRESS
    ] == ["context.preparing", "model.started", "model.synthesizing"]
    await runtime.aclose()


def test_memory_capture_runs_once_for_cli_and_gateway_but_not_scheduler() -> None:
    asyncio.run(_assert_memory_capture_source_boundary())


async def _assert_memory_capture_source_boundary() -> None:
    for source, expected_calls in (
        (InvocationSource.CLI, 1),
        (InvocationSource.GATEWAY, 1),
        (InvocationSource.SCHEDULER, 0),
    ):
        capture = RecordingMemoryCapture([])
        runtime, _, _, _ = _runtime((_response("done"),), memory_capture=capture)
        handle = await runtime.submit(
            RuntimeRequest(
                "request_001",
                "session_001",
                source,
                UserInput("remember this"),
                scheduler_task_id="task_001" if source is InvocationSource.SCHEDULER else None,
                occurrence_id="occurrence_001"
                if source is InvocationSource.SCHEDULER
                else None,
            )
        )

        events = [event async for event in handle.events()]

        assert len(capture.calls) == expected_calls
        assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED
        assert all("memory" not in event.event_type.value for event in events)
        await runtime.aclose()


def test_memory_capture_runs_after_assistant_is_saved_and_emitted() -> None:
    asyncio.run(_assert_memory_capture_order())


async def _assert_memory_capture_order() -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "test", (), "", NOW, NOW))
    emitted: list[RuntimeEventType] = []

    class OrderedCapture:
        calls = 0

        async def capture(self, session: Session, request: RuntimeRequest) -> None:
            self.calls += 1
            stored = sessions.load(request.session_id)
            assert stored == session
            assert stored.messages[-1].role is MessageRole.ASSISTANT
            assert stored.messages[-1].content == "saved answer"
            assert emitted[-1] is RuntimeEventType.ASSISTANT_MESSAGE

    capture = OrderedCapture()
    loop = AgentLoop(
        llm=FakeLLMClient([_response("saved answer")]),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        memory_capture=cast(MemoryCapture, capture),
    )

    await loop.run_locked(_request(), lambda event_type, _: emitted.append(event_type))

    assert capture.calls == 1


@pytest.mark.parametrize(
    "error",
    (
        LLMError("provider unavailable"),
        StoreError("store unavailable"),
        MemoryExtractionError("invalid response"),
        ValueError("invalid operation"),
    ),
)
def test_memory_capture_failure_does_not_fail_successful_run(error: Exception) -> None:
    asyncio.run(_assert_memory_capture_failure_is_silent(error))


async def _assert_memory_capture_failure_is_silent(error: Exception) -> None:
    capture = RecordingMemoryCapture([], error)
    runtime, sessions, _, _ = _runtime((_response("done"),), memory_capture=capture)

    events = await _events(runtime)
    stored = sessions.load("session_001")

    assert len(capture.calls) == 1
    assert stored.messages[-1].content == "done"
    assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED
    assert all(event.event_type is not RuntimeEventType.RUN_FAILED for event in events)
    await runtime.aclose()


def test_memory_capture_uses_current_run_user_and_tool_results_not_steer() -> None:
    asyncio.run(_assert_memory_capture_provenance())


async def _assert_memory_capture_provenance() -> None:
    current_entry = MemoryEntry(
        "memory_001",
        "existing",
        NOW,
        category=MemoryCategory.PREFERENCE,
    )
    operation = MemoryOperation(
        MemoryOperationType.ADD,
        content="candidate",
        category=MemoryCategory.PREFERENCE,
        importance=4,
    )

    class Policy:
        def get(self) -> MemoryPolicySnapshot:
            return MemoryPolicySnapshot("rules", 1, False, None)

    class Retriever:
        queries: list[str] = []

        def retrieve(self, query: str) -> tuple[MemoryEntry, ...]:
            self.queries.append(query)
            return (current_entry,)

    class Extractor:
        calls: list[
            tuple[MemoryTurn, str, tuple[MemoryEntry, ...]]
        ] = []

        async def extract(
            self,
            turn: MemoryTurn,
            *,
            rules: str,
            existing: tuple[MemoryEntry, ...],
        ) -> tuple[MemoryOperation, ...]:
            self.calls.append((turn, rules, existing))
            return (operation,)

    class Service:
        calls: list[tuple[tuple[MemoryOperation, ...], str, str]] = []

        def apply_automatic_batch(
            self,
            operations: tuple[MemoryOperation, ...],
            *,
            source_session_id: str,
            source_message_id: str,
        ) -> tuple[MemoryEntry, ...]:
            self.calls.append(
                (operations, source_session_id, source_message_id)
            )
            return ()

    retriever = Retriever()
    extractor = Extractor()
    service = Service()
    capture = MemoryCapture(
        policy=cast(MemoryPolicyService, Policy()),
        retriever=cast(LocalMemoryRetriever, retriever),
        extractor=cast(MemoryExtractor, extractor),
        service=cast(MemoryService, service),
    )
    messages = (
        Message("message_001", MessageRole.USER, "same text", NOW, 1),
        Message("message_002", MessageRole.TOOL, "old tool", NOW, 2, "old_call"),
        Message("message_003", MessageRole.ASSISTANT, "old answer", NOW, 3),
        Message("message_004", MessageRole.USER, "same text", NOW, 4),
        Message(
            "message_005",
            MessageRole.USER,
            "steer text",
            NOW,
            5,
            input_kind=UserInputKind.STEER,
        ),
        Message("message_006", MessageRole.TOOL, "current tool", NOW, 6, "call_001"),
        Message("message_007", MessageRole.ASSISTANT, "final answer", NOW, 7),
    )
    session = Session(
        "session_001",
        "test",
        messages,
        "",
        NOW,
        NOW,
        next_sequence_number=8,
        runs=(
            RunState(
                "run_001",
                "request_001",
                RunStatus.COMPLETED,
                started_at=NOW,
                completed_at=NOW,
                duration_ms=0,
                assistant_message_id="message_007",
            ),
        ),
    )

    await capture.capture(session, _request())

    assert retriever.queries == ["same text"]
    assert extractor.calls == [
        (
            MemoryTurn("same text", "final answer", ("current tool",)),
            "rules",
            (current_entry,),
        )
    ]
    assert service.calls == [
        ((operation,), "session_001", "message_004")
    ]


@pytest.mark.parametrize(
    "error",
    (
        LLMError("candidate secret"),
        StoreError("candidate secret"),
        MemoryExtractionError("candidate secret"),
        ValueError("candidate secret"),
    ),
)
def test_memory_capture_swallows_expected_failures_without_candidate_log(
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    asyncio.run(_assert_memory_capture_safe_diagnostic(error, caplog))


async def _assert_memory_capture_safe_diagnostic(
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Policy:
        def get(self) -> MemoryPolicySnapshot:
            return MemoryPolicySnapshot("rules", 1, False, None)

    class Retriever:
        def retrieve(self, query: str) -> tuple[MemoryEntry, ...]:
            return ()

    class Extractor:
        async def extract(
            self,
            turn: MemoryTurn,
            *,
            rules: str,
            existing: tuple[MemoryEntry, ...],
        ) -> tuple[MemoryOperation, ...]:
            raise error

    class Service:
        def apply_automatic_batch(
            self,
            operations: tuple[MemoryOperation, ...],
            *,
            source_session_id: str,
            source_message_id: str,
        ) -> tuple[MemoryEntry, ...]:
            raise AssertionError("failed extraction must not reach service")

    capture = MemoryCapture(
        policy=cast(MemoryPolicyService, Policy()),
        retriever=cast(LocalMemoryRetriever, Retriever()),
        extractor=cast(MemoryExtractor, Extractor()),
        service=cast(MemoryService, Service()),
    )
    session = Session(
        "session_001",
        "test",
        (
            Message("message_001", MessageRole.USER, "user secret", NOW, 1),
            Message("message_002", MessageRole.ASSISTANT, "answer secret", NOW, 2),
        ),
        "",
        NOW,
        NOW,
        next_sequence_number=3,
        runs=(
            RunState(
                "run_001",
                "request_001",
                RunStatus.COMPLETED,
                started_at=NOW,
                completed_at=NOW,
                duration_ms=0,
                assistant_message_id="message_002",
            ),
        ),
    )

    with caplog.at_level(logging.WARNING):
        await capture.capture(session, _request())

    assert "candidate secret" not in caplog.text
    assert "user secret" not in caplog.text
    assert "answer secret" not in caplog.text
