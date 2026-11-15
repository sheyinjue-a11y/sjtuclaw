"""Gateway RuntimeHost and SSE contracts without a live provider."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from sjtuclaw.entrypoints.gateway import GatewayOptions, _sse_frame, create_gateway
from sjtuclaw.entrypoints.gateway_streams import _public_payload
from sjtuclaw.infrastructure.persistence import InMemorySessionStore, JsonAttachmentStore
from sjtuclaw.models.message import Message, MessageRole
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
from sjtuclaw.models.skill import SkillManifest, SkillReference
from sjtuclaw.models.tool import ToolCall, ToolResourceLink, ToolResultStatus
from sjtuclaw.runtime import (
    AttachmentService,
    CommandService,
    PromptConfigurationService,
    RunHandle,
    RuntimeHost,
    SessionCoordinator,
    SessionService,
)
from tests.fakes import FakeClock, FakeIdGenerator

NOW = datetime(2026, 7, 20, tzinfo=UTC)


def test_public_payload_unfreezes_nested_structured_content() -> None:
    """RuntimeHost freezes payloads; the SSE projection must stay JSON-safe."""

    request = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.GATEWAY,
        UserInput("list"),
    )
    event = RuntimeHost._event(
        RuntimeEventType.TOOL_RESULT,
        request,
        1,
        {
            "call_id": "call_001",
            "tool": "list_dir",
            "content": "notes.md\nreports/",
            "status": "success",
            "error_code": None,
            "structured_content": {
                "path": ".",
                "entries": [
                    {"name": "notes.md", "type": "file"},
                    {"name": "reports", "type": "directory"},
                ],
            },
            "resources": [],
        },
    )

    frame = _sse_frame("tool_result", _public_payload(event), 1)
    assert '"entries": [{"name": "notes.md", "type": "file"},' in frame
    assert '"name": "reports", "type": "directory"}]' in frame


def test_gateway_lists_and_executes_shared_commands() -> None:
    sessions, _store, session = _sessions()
    commands = CommandService(sessions=sessions)
    options = GatewayOptions(port=8765, bootstrap_token="c" * 43)

    with TestClient(
        create_gateway(sessions=sessions, commands=commands, options=options),
        base_url=options.origin,
    ) as client:
        unauthenticated = client.get("/api/v1/commands")
        _bootstrap(client, options)
        catalog = client.get("/api/v1/commands")
        listed = client.post(
            f"/api/v1/sessions/{session.session_id}/commands",
            headers={"Origin": options.origin},
            json={"command": "/command"},
        )
        wrong_origin = client.post(
            f"/api/v1/sessions/{session.session_id}/commands",
            headers={"Origin": "http://invalid.example"},
            json={"command": "/command"},
        )
        unknown = client.post(
            f"/api/v1/sessions/{session.session_id}/commands",
            headers={"Origin": options.origin},
            json={"command": "/does-not-exist"},
        )
        missing = client.post(
            "/api/v1/sessions/session_missing/commands",
            headers={"Origin": options.origin},
            json={"command": "/command"},
        )
        _store.save(
            replace(
                session,
                archived_at=NOW,
                archived_reason=SessionArchiveReason.USER_ARCHIVED,
            )
        )
        archived = client.post(
            f"/api/v1/sessions/{session.session_id}/commands",
            headers={"Origin": options.origin},
            json={"command": "/command"},
        )

    assert unauthenticated.status_code == 401
    assert catalog.status_code == 200
    assert "/skill show <skill-name>" in {
        item["template"] for item in catalog.json()["commands"]
    }
    assert "/exit" not in {item["template"] for item in catalog.json()["commands"]}
    assert listed.status_code == 200
    assert listed.json()["outcome"]["isError"] is False
    assert wrong_origin.status_code == 403
    assert unknown.status_code == 200
    assert unknown.json()["outcome"]["isError"] is True
    assert missing.status_code == 404
    assert archived.status_code == 409


def test_gateway_command_submits_explicit_skill_run() -> None:
    class Skills:
        def __init__(self) -> None:
            self.manifest = SkillManifest(
                skill_id="course-report",
                description="write a course report",
                version="1.0.0",
                snapshot_id="course-report-1111111111111111",
                snapshot_hash="a" * 64,
                available=True,
            )

        def list(self) -> tuple[SkillManifest, ...]:
            return (self.manifest,)

        def refresh(self) -> tuple[SkillManifest, ...]:
            return self.list()

        def resolve_current_names(self, names: tuple[str, ...]) -> tuple[SkillReference, ...]:
            assert names == ("course-report",)
            return (self.manifest.reference,)

    sessions, _store, session = _sessions()
    runtime = _GatewayRuntime(session.session_id)
    commands = CommandService(sessions=sessions, skills=Skills())  # type: ignore[arg-type]
    options = GatewayOptions(port=8765, bootstrap_token="d" * 43)

    with TestClient(
        create_gateway(
            sessions=sessions,
            runtime=cast(RuntimeHost, runtime),
            commands=commands,
            options=options,
        ),
        base_url=options.origin,
    ) as client:
        _bootstrap(client, options)
        submitted = client.post(
            f"/api/v1/sessions/{session.session_id}/commands",
            headers={"Origin": options.origin},
            json={
                "command": "/skill course-report summarize this lecture",
                "idempotencyKey": "command-key-001",
            },
        )

    assert submitted.status_code == 202
    assert submitted.json()["outcome"]["isError"] is False
    assert submitted.json()["request"]["runId"] == "run_001"
    assert runtime.requests[0].user_input.text == "summarize this lecture"
    assert runtime.requests[0].skill_refs == (Skills().manifest.reference,)


def test_gateway_reads_prompt_metadata_and_updates_only_soul(tmp_path: Path) -> None:
    sessions, _store, session = _sessions()
    prompts = PromptConfigurationService(
        root=tmp_path / "prompts",
        default_system_prompt="system rules",
        default_soul="initial soul",
    )
    options = GatewayOptions(port=8765, bootstrap_token="p" * 43)

    with TestClient(
        create_gateway(sessions=sessions, prompts=prompts, options=options),
        base_url=options.origin,
    ) as client:
        _bootstrap(client, options)
        initial = client.get("/api/v1/settings/prompts")
        updated = client.put(
            "/api/v1/settings/prompts/soul",
            headers={"Origin": options.origin},
            json={"content": "updated soul"},
        )
        forbidden = client.put(
            "/api/v1/settings/prompts/system",
            headers={"Origin": options.origin},
            json={"content": "must not be editable"},
        )

    assert initial.status_code == 200
    assert initial.json() == {
        "systemPrompt": {
            "path": str(tmp_path / "prompts" / "system_prompt.md"),
            "editable": False,
            "restartRequired": True,
        },
        "soul": {
            "path": str(tmp_path / "prompts" / "soul.md"),
            "content": "initial soul",
        },
    }
    assert updated.status_code == 200
    assert updated.json()["soul"]["content"] == "updated soul"
    assert prompts.snapshot().soul == "updated soul"
    assert forbidden.status_code == 405


def test_gateway_submits_and_controls_runtime_through_public_contract() -> None:
    sessions, _store, session = _sessions()
    runtime = _GatewayRuntime(session.session_id)
    options = GatewayOptions(port=8765, bootstrap_token="r" * 43)

    with TestClient(
        create_gateway(sessions=sessions, runtime=cast(RuntimeHost, runtime), options=options),
        base_url=options.origin,
    ) as client:
        _bootstrap(client, options)
        submitted = client.post(
            f"/api/v1/sessions/{session.session_id}/requests",
            headers={"Origin": options.origin},
            json={
                "text": "  请处理附件  ",
                "attachmentRefs": ["attachment_001"],
                "idempotencyKey": "client-key-001",
            },
        )
        assert submitted.status_code == 202
        assert submitted.json()["request"] == {
            "runId": "run_001",
            "requestId": runtime.requests[0].request_id,
            "status": "queued",
            "startedAt": None,
            "completedAt": None,
            "durationMs": None,
            "assistantMessageId": None,
            "selectedSkillRefs": [],
        }
        assert runtime.requests[0].source is InvocationSource.GATEWAY
        assert runtime.requests[0].user_input.text == "请处理附件"
        assert runtime.requests[0].attachment_refs == ("attachment_001",)
        assert runtime.requests[0].idempotency_key == "client-key-001"
        _store.save(replace(session, runs=(runtime.run,)))

        cancelled = client.delete(
            f"/api/v1/sessions/{session.session_id}/requests/{runtime.requests[0].request_id}",
            headers={"Origin": options.origin},
        )
        assert cancelled.status_code == 200
        assert runtime.cancelled == [(session.session_id, runtime.requests[0].request_id)]

        steered = client.post(
            f"/api/v1/sessions/{session.session_id}/runs/run_001/steer",
            headers={"Origin": options.origin},
            json={
                "steerId": "steer_001",
                "idempotencyKey": "steer-key-001",
                "text": "改为总结",
            },
        )
        assert steered.status_code == 200
        assert steered.json() == {
            "steer": {"steerId": "steer_001", "status": "STEER_ACCEPTED"}
        }
        assert runtime.steers == [
            (session.session_id, "run_001", "steer_001", "steer-key-001", "改为总结")
        ]

        queued_guidance = client.post(
            f"/api/v1/sessions/{session.session_id}/runs/run_001"
            "/queued-requests/request_queued/steer",
            headers={"Origin": options.origin},
            json={
                "steerId": "steer_queued",
                "idempotencyKey": "steer-queued-key",
            },
        )
        assert queued_guidance.status_code == 200
        assert runtime.queued_steers == [
            (
                session.session_id,
                "run_001",
                "request_queued",
                "steer_queued",
                "steer-queued-key",
            )
        ]


def test_gateway_session_snapshot_exposes_local_observability_fields() -> None:
    sessions, store, session = _sessions()
    call = ToolCall("call_001", "write_file", '{"path":"notes.txt","content":"内容"}')
    download_call = ToolCall("call_002", "create_download", '{"path":"report.md"}')
    resource = ToolResourceLink(
        kind="download",
        name="report.md",
        href="/downloads/download_123",
        mime_type="text/markdown",
        expires_at="2026-07-31T00:00:00+00:00",
    )
    assistant = Message(
        message_id="message_001",
        role=MessageRole.ASSISTANT,
        content="准备写入",
        created_at=NOW,
        sequence_number=1,
        reasoning_content="需要先创建课程笔记",
        tool_calls=(call, download_call),
    )
    tool = Message(
        message_id="message_002",
        role=MessageRole.TOOL,
        content="workspace is not configured",
        created_at=NOW,
        sequence_number=2,
        tool_call_id=call.call_id,
        tool_name=call.name,
        tool_result_status=ToolResultStatus.ERROR,
        tool_error_code="WORKSPACE_NOT_CONFIGURED",
    )
    download = Message(
        message_id="message_003",
        role=MessageRole.TOOL,
        content="report ready",
        created_at=NOW,
        sequence_number=3,
        tool_call_id=download_call.call_id,
        tool_name=download_call.name,
        tool_result_status=ToolResultStatus.SUCCESS,
        tool_structured_content={"filename": "report.md", "size_bytes": 12},
        tool_resources=(resource,),
    )
    pending = PendingRequest(
        request_id="request_queued",
        acceptance_sequence=1,
        source=InvocationSource.GATEWAY,
        actor_id=None,
        text="生成课程总结",
        submitted_at=NOW,
    )
    run = RunState("run_queued", pending.request_id, RunStatus.QUEUED)
    store.save(replace(
        session,
        messages=(assistant, tool, download),
        next_sequence_number=4,
        pending_requests=(pending,),
        runs=(run,),
    ))
    options = GatewayOptions(port=8765, bootstrap_token="o" * 43)

    with TestClient(create_gateway(sessions=sessions, options=options), base_url=options.origin) as client:
        _bootstrap(client, options)
        payload = client.get(f"/api/v1/sessions/{session.session_id}").json()["session"]

    assert payload["messages"][0]["reasoningContent"] == "需要先创建课程笔记"
    assert payload["messages"][0]["toolCalls"] == [
        {
            "callId": "call_001",
            "name": "write_file",
            "arguments": '{"path":"notes.txt","content":"内容"}',
        },
        {
            "callId": "call_002",
            "name": "create_download",
            "arguments": '{"path":"report.md"}',
        },
    ]
    assert payload["messages"][1]["toolResultStatus"] == "error"
    assert payload["messages"][2]["structuredContent"] == {
        "filename": "report.md",
        "size_bytes": 12,
    }
    assert payload["messages"][2]["resources"] == [
        {
            "kind": "download",
            "name": "report.md",
            "href": "/downloads/download_123",
            "mimeType": "text/markdown",
            "expiresAt": "2026-07-31T00:00:00+00:00",
        }
    ]
    assert payload["pendingRequests"][0]["text"] == "生成课程总结"


def test_gateway_sse_replays_stable_ids_without_provider_reasoning() -> None:
    sessions, store, session = _sessions()
    run = RunState("run_001", "request_001", RunStatus.QUEUED)
    store.save(replace(session, runs=(run,)))
    runtime = _GatewayRuntime(session.session_id)
    runtime.handle.publish(
        RuntimeEvent(
            RuntimeEventType.REQUEST_ACCEPTED,
            session.session_id,
            run.request_id,
            1,
            InvocationSource.GATEWAY,
        )
    )
    runtime.handle.publish(
        RuntimeEvent(
            RuntimeEventType.ASSISTANT_DELTA,
            session.session_id,
            run.request_id,
            1,
            InvocationSource.GATEWAY,
            {"delta": "安全增量", "reasoning_content": "不得泄露"},
        )
    )
    runtime.handle.publish(
        RuntimeEvent(
            RuntimeEventType.RUN_PROGRESS,
            session.session_id,
            run.request_id,
            1,
            InvocationSource.GATEWAY,
            {
                "code": "skill.selected",
                "skill_id": "course-report",
                "source": "auto",
                "reason": "the request needs a structured course report",
                "private": "不得透传",
            },
        )
    )
    runtime.handle.publish(
        RuntimeEvent(
            RuntimeEventType.TOOL_CALL,
            session.session_id,
            run.request_id,
            1,
            InvocationSource.GATEWAY,
            {
                "call_id": "call_001",
                "name": "create_download",
                "arguments": '{"path":"report.md"}',
                "private": "不得透传",
            },
        )
    )
    runtime.handle.publish(
        RuntimeEvent(
            RuntimeEventType.TOOL_RESULT,
            session.session_id,
            run.request_id,
            1,
            InvocationSource.GATEWAY,
            {
                "call_id": "call_001",
                "tool": "create_download",
                "status": "success",
                "content": "report ready",
                "error_code": None,
                "structured_content": {
                    "filename": "report.md",
                    "size_bytes": 12,
                },
                "resources": [
                    {
                        "kind": "download",
                        "name": "report.md",
                        "href": "/downloads/download_123",
                        "mime_type": "text/markdown",
                        "expires_at": "2026-07-31T00:00:00+00:00",
                    },
                    {
                        "kind": "download",
                        "name": "evil.md",
                        "href": "https://evil.example/download_456",
                        "mime_type": "text/markdown",
                        "expires_at": None,
                    },
                    {
                        "kind": "preview",
                        "name": "preview.md",
                        "href": "/downloads/download_789",
                        "mime_type": "text/markdown",
                        "expires_at": None,
                    },
                ],
                "private": "不得透传",
            },
        )
    )
    runtime.handle.publish(
        RuntimeEvent(
            RuntimeEventType.TOOL_RESULT,
            session.session_id,
            run.request_id,
            1,
            InvocationSource.GATEWAY,
            {
                "call_id": "call_002",
                "tool": "create_download",
                "status": "error",
                "content": "download failed",
                "error_code": "DOWNLOAD_FAILED",
                "structured_content": None,
                "resources": [
                    {
                        "kind": "download",
                        "name": "error.md",
                        "href": "/downloads/error_123",
                        "mime_type": "text/markdown",
                        "expires_at": None,
                    }
                ],
            },
        )
    )
    runtime.handle.publish(
        RuntimeEvent(
            RuntimeEventType.RUN_COMPLETED,
            session.session_id,
            run.request_id,
            1,
            InvocationSource.GATEWAY,
        )
    )
    options = GatewayOptions(port=8765, bootstrap_token="s" * 43)

    with TestClient(
        create_gateway(sessions=sessions, runtime=cast(RuntimeHost, runtime), options=options),
        base_url=options.origin,
    ) as client:
        _bootstrap(client, options)
        stream_path = f"/api/v1/sessions/{session.session_id}/runs/{run.run_id}/events"
        cross_origin = client.get(
            stream_path,
            headers={"Origin": "http://localhost:8765"},
        )
        assert cross_origin.status_code == 403
        assert cross_origin.json()["code"] == "INVALID_ORIGIN"

        first = _sse_text(
            client,
            stream_path,
        )
        assert "event: snapshot" in first
        assert "id: 1" in first
        assert "id: 2" in first
        assert "id: 3" in first
        assert "id: 4" in first
        assert "id: 5" in first
        assert "id: 6" in first
        assert "id: 7" in first
        assert '"delta": "安全增量"' in first
        assert '"skillId": "course-report"' in first
        assert '"source": "auto"' in first
        assert '"reason": "the request needs a structured course report"' in first
        assert '"input": "{\\"path\\":\\"report.md\\"}"' in first
        assert '"output": "report ready"' in first
        assert '"structuredContent": {"filename": "report.md", "size_bytes": 12}' in first
        assert (
            '"resources": [{"kind": "download", "name": "report.md", '
            '"href": "/downloads/download_123", "mimeType": "text/markdown", '
            '"expiresAt": "2026-07-31T00:00:00+00:00"}]'
        ) in first
        assert "evil.example" not in first
        assert "preview.md" not in first
        assert (
            '"callId": "call_002", "tool": "create_download", "status": "error", '
            '"output": "download failed", "errorCode": "DOWNLOAD_FAILED", '
            '"structuredContent": null, "resources": []'
        ) in first
        assert "error.md" not in first
        assert "/downloads/error_123" not in first
        assert "不得泄露" not in first
        assert "不得透传" not in first
        assert first.index("event: request_accepted") < first.index("event: assistant_delta")
        assert first.index("event: assistant_delta") < first.index("event: tool_call")
        assert first.index("event: tool_call") < first.index("event: tool_result")
        assert first.rindex("event: tool_result") < first.index("event: run_completed")

        replay = _sse_text(
            client,
            stream_path,
            last_event_id="4",
        )
        assert "event: snapshot" in replay
        assert "id: 5" in replay
        assert "id: 1" not in replay
        assert "id: 2" not in replay
        assert "id: 3" not in replay
        assert "id: 4" not in replay


def test_gateway_streams_text_attachment_to_attachment_service(tmp_path: Path) -> None:
    sessions, attachments, session = _attachment_services(tmp_path)
    options = GatewayOptions(port=8765, bootstrap_token="a" * 43)

    with TestClient(
        create_gateway(sessions=sessions, attachments=attachments, options=options),
        base_url=options.origin,
    ) as client:
        _bootstrap(client, options)
        uploaded = client.post(
            f"/api/v1/sessions/{session.session_id}/attachments",
            headers={
                "Origin": options.origin,
                "Content-Type": "text/plain; charset=utf-8",
                "X-Attachment-Name": "notes.txt",
            },
            content=b"line one\r\nline two",
        )
        assert uploaded.status_code == 201
        attachment = uploaded.json()["attachment"]
        assert attachment["kind"] == "text"
        assert attachment["status"] == "ready"
        assert attachment["displayName"] == "notes.txt"

        listed = client.get(f"/api/v1/sessions/{session.session_id}/attachments")
        assert listed.status_code == 200
        assert listed.json()["attachments"] == [attachment]

        unsupported = client.post(
            f"/api/v1/sessions/{session.session_id}/attachments",
            headers={
                "Origin": options.origin,
                "Content-Type": "application/octet-stream",
                "X-Attachment-Name": "notes.bin",
            },
            content=b"not accepted",
        )
        assert unsupported.status_code == 415
        assert unsupported.json()["code"] == "ATTACHMENT_UNSUPPORTED_TYPE"

        deleted = client.delete(
            f"/api/v1/sessions/{session.session_id}/attachments/{attachment['attachmentId']}",
            headers={"Origin": options.origin},
        )
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/sessions/{session.session_id}/attachments").json() == {
            "attachments": []
        }


class _GatewayRuntime:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self.requests: list[RuntimeRequest] = []
        self.cancelled: list[tuple[str, str]] = []
        self.steers: list[tuple[str, str, str, str, str]] = []
        self.queued_steers: list[tuple[str, str, str, str, str]] = []
        self.run = RunState("run_001", "request_001", RunStatus.QUEUED)
        cancelled = RuntimeEvent(
            RuntimeEventType.RUN_CANCELLED,
            session_id,
            "request_001",
            1,
            InvocationSource.GATEWAY,
        )
        self.handle = RunHandle(cancelled)

    async def submit(self, request: RuntimeRequest) -> RunHandle:
        self.requests.append(request)
        self.run = RunState("run_001", request.request_id, RunStatus.QUEUED)
        return self.handle

    def find_run(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunState | None:
        if session_id != self._session_id:
            return None
        if request_id == self.run.request_id or idempotency_key is not None:
            return self.run
        return None

    async def cancel_queued(self, session_id: str, request_id: str) -> bool:
        self.cancelled.append((session_id, request_id))
        self.run = replace(self.run, status=RunStatus.CANCELLED_BEFORE_START)
        return True

    async def stop_run(self, session_id: str, run_id: str) -> bool:
        return session_id == self._session_id and run_id == self.run.run_id

    async def steer(
        self,
        session_id: str,
        run_id: str,
        *,
        steer_id: str,
        idempotency_key: str,
        text: str,
    ) -> str:
        self.steers.append((session_id, run_id, steer_id, idempotency_key, text))
        return "STEER_ACCEPTED"

    async def steer_queued(
        self,
        session_id: str,
        run_id: str,
        *,
        request_id: str,
        steer_id: str,
        idempotency_key: str,
    ) -> str:
        self.queued_steers.append(
            (session_id, run_id, request_id, steer_id, idempotency_key)
        )
        return "STEER_ACCEPTED"

    def handle_for_run(self, session_id: str, run_id: str) -> RunHandle | None:
        if session_id != self._session_id or run_id != "run_001":
            return None
        return self.handle


def _sessions() -> tuple[SessionService, InMemorySessionStore, Session]:
    store = InMemorySessionStore()
    clock = FakeClock(NOW)
    service = SessionService(
        sessions=store,
        coordinator=SessionCoordinator(),
        clock=clock,
        ids=FakeIdGenerator(),
    )
    return service, store, service.create("测试")


def _attachment_services(tmp_path: Path) -> tuple[SessionService, AttachmentService, Session]:
    store = InMemorySessionStore()
    coordinator = SessionCoordinator()
    service = SessionService(
        sessions=store,
        coordinator=coordinator,
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    attachments = AttachmentService(
        sessions=store,
        attachments=JsonAttachmentStore(tmp_path),
        coordinator=coordinator,
    )
    return service, attachments, service.create("附件")


def _bootstrap(client: TestClient, options: GatewayOptions) -> None:
    response = client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": options.origin},
        json={"token": options.bootstrap_token},
    )
    assert response.status_code == 204


def _sse_text(
    client: TestClient,
    path: str,
    *,
    last_event_id: str | None = None,
) -> str:
    headers = {"Sec-Fetch-Site": "same-origin"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    with client.stream("GET", path, headers=headers) as response:
        assert response.status_code == 200
        return "".join(response.iter_text())
