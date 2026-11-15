"""Composition-root coverage for the Step 5 read-only tool loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

import pytest

from sjtuclaw import bootstrap
from sjtuclaw.config import Settings
from sjtuclaw.infrastructure.llm.tool_protocol import native_messages
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMToolMode, LLMUsage
from sjtuclaw.models.runtime import (
    InvocationSource,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRequest,
    UserInput,
)
from sjtuclaw.models.session import ApprovalDecisionStatus
from sjtuclaw.models.tool import ToolCall
from sjtuclaw.runtime import MemoryPolicyService, MemoryService, RunHandle


def test_bootstrap_wires_read_only_tools_permission_and_native_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_assert_bootstrap_wires_tools(tmp_path, monkeypatch))


async def _assert_bootstrap_wires_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDeepSeek:
        instances: ClassVar[list[StubDeepSeek]] = []

        def __init__(
            self,
            settings: Settings,
            *,
            thinking_enabled: bool,
            ids: object,
        ) -> None:
            self.ids = ids
            self.thinking_enabled = thinking_enabled
            self.requests: list[LLMRequest] = []
            self.closed = False
            self.instances.append(self)

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                return LLMResponse(
                    "",
                    LLMUsage(12, 3, 15),
                    "tool_calls",
                    tool_calls=(
                        ToolCall("call_001", "read_file", '{"path":"note.txt"}'),
                    ),
                )
            if len(self.requests) == 2:
                return LLMResponse("tool marker", LLMUsage(18, 4, 22), "stop")
            return LLMResponse("读取笔记并回答", LLMUsage(3, 2, 5), "stop")

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(bootstrap, "DeepSeekLLMClient", StubDeepSeek)
    (tmp_path / "note.txt").write_text("safe note", encoding="utf-8")
    settings = Settings(
        api_key="test-key",
        data_dir=tmp_path / "data",
        tool_root=tmp_path,
        tool_mode="native",
        token_counter_mode="approximate",
    )
    application = bootstrap.build_cli_application(
        settings,
        "system prompt",
        "",
        "summarize old turns",
    )
    request = RuntimeRequest(
        "request_001",
        application.initial_session_id,
        InvocationSource.GATEWAY,
        UserInput("read note.txt then answer"),
    )
    try:
        events = await _collect(await application.runtime.submit(request))
    finally:
        await application.aclose()

    client = StubDeepSeek.instances[0]
    assert client.closed is True
    assert client.thinking_enabled is True
    assert [definition.name for definition in client.requests[0].tools] == [
        "current_time",
        "list_dir",
        "read_file",
        "read_attachment",
        "copy_attachment_to_workspace",
        "write_file",
        "create_download",
        "new_shell",
        "run_command",
        "read_skill_resource",
        "select_skill",
    ]
    assert client.requests[0].tool_mode is LLMToolMode.NATIVE
    result = next(event for event in events if event.event_type is RuntimeEventType.TOOL_RESULT)
    assert result.payload["content"] == "safe note"
    assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED
    assert len(client.requests) == 4
    assert isinstance(application.memory_service, MemoryService)
    assert isinstance(application.memory_policy, MemoryPolicyService)
    assert (
        application.session_service.load(application.initial_session_id).title
        == "读取笔记并回答"
    )


def test_bootstrap_create_download_keeps_resource_host_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_assert_bootstrap_create_download_keeps_resource_host_only(tmp_path, monkeypatch))


async def _assert_bootstrap_create_download_keeps_resource_host_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubCounter:
        def count(self, request: LLMRequest) -> int:
            del request
            return 1

    class StubDeepSeek:
        instances: ClassVar[list[StubDeepSeek]] = []

        def __init__(
            self,
            settings: Settings,
            *,
            thinking_enabled: bool,
            ids: object,
        ) -> None:
            del settings, thinking_enabled, ids
            self.requests: list[LLMRequest] = []
            self.serialized_requests: list[list[dict[str, object]]] = []
            self.instances.append(self)

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            self.serialized_requests.append(native_messages(request.messages, request.tools))
            if len(self.requests) == 1:
                return LLMResponse(
                    "",
                    LLMUsage(10, 2, 12),
                    "tool_calls",
                    tool_calls=(
                        ToolCall(
                            "call_download",
                            "create_download",
                            '{"path":"report.md"}',
                        ),
                    ),
                )
            return LLMResponse("下载已创建", LLMUsage(10, 2, 12), "stop")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(bootstrap, "DeepSeekLLMClient", StubDeepSeek)
    monkeypatch.setattr(bootstrap, "DeepSeekTokenCounter", lambda _path: StubCounter())
    (tmp_path / "report.md").write_text("host-only report", encoding="utf-8")
    settings = Settings(
        api_key="test-key",
        data_dir=tmp_path / "data",
        tool_root=tmp_path,
        tool_mode="native",
    )
    application = bootstrap.build_cli_application(
        settings,
        "system prompt",
        "",
        "summarize old turns",
    )
    await application.workspace_service.bind(application.initial_session_id, tmp_path)
    handle = await application.runtime.submit(
        RuntimeRequest(
            "request_download",
            application.initial_session_id,
            InvocationSource.GATEWAY,
            UserInput("create a download for report.md"),
        )
    )
    try:
        for _ in range(100):
            pending = application.session_service.load(
                application.initial_session_id
            ).pending_approval
            if pending is not None:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("create_download approval was not persisted")

        await application.runtime.decide_approval(
            application.initial_session_id,
            pending.approval_id,
            status=ApprovalDecisionStatus.APPROVED,
            idempotency_key="approve_download",
            actor_id="test-user",
        )
        events = await _collect(handle)
        stored = application.session_service.load(application.initial_session_id)
    finally:
        await application.aclose()

    result_event = next(
        event
        for event in events
        if event.event_type is RuntimeEventType.TOOL_RESULT
        and event.payload["tool"] == "create_download"
    )
    resources = result_event.payload["resources"]
    assert isinstance(resources, tuple)
    assert len(resources) == 1
    resource = resources[0]
    assert isinstance(resource, Mapping)
    assert resource["kind"] == "download"
    assert resource["name"] == "report.md"
    href = resource["href"]
    assert isinstance(href, str)
    assert href.startswith("/downloads/download_")

    tool_message = next(
        message
        for message in stored.messages
        if message.tool_name == "create_download"
    )
    assert tool_message.tool_resources[0].href == href
    assert tool_message.tool_structured_content is not None
    assert tool_message.tool_structured_content["filename"] == "report.md"

    download_id = href.removeprefix("/downloads/")
    model_request = json.dumps(
        StubDeepSeek.instances[0].serialized_requests[1],
        ensure_ascii=False,
    )
    assert "下载快照已创建" in model_request
    assert '\\"filename\\":\\"report.md\\"' in model_request
    assert "structured_content" in model_request
    assert '"resources"' not in model_request
    assert '\\"resources\\"' not in model_request
    assert '"href"' not in model_request
    assert '\\"href\\"' not in model_request
    assert download_id not in model_request


async def _collect(handle: RunHandle) -> list[RuntimeEvent]:
    events: list[RuntimeEvent] = []
    async for event in handle.events():
        events.append(event)
    return events
