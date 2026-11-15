"""JSON-specific persistence behavior beyond the reusable Store contract."""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.infrastructure.persistence import JsonMemoryStore, JsonSessionStore
from sjtuclaw.models.memory import Memory, MemoryCategory, MemoryEntry, MemorySourceKind
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.runtime import InvocationSource
from sjtuclaw.models.session import (
    ApprovalDecision,
    ApprovalDecisionStatus,
    PendingApproval,
    PendingRequest,
    RunCheckpoint,
    RunState,
    RunStatus,
    Session,
    WorkspaceBinding,
)
from sjtuclaw.models.skill import SkillReference
from sjtuclaw.models.tool import (
    ToolCall,
    ToolIntent,
    ToolResourceLink,
    ToolResultStatus,
    ToolSafetyLevel,
)

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def make_session(session_id: str, title: str = "测试") -> Session:
    return Session(session_id, title, (), "", NOW, NOW)


def only_session_file(root: Path) -> Path:
    files = list((root / "sessions").glob("*.json"))
    assert len(files) == 1
    return files[0]


def drop_v5_session_fields(document: dict[str, object]) -> None:
    session = document["session"]
    assert isinstance(session, dict)
    for field in (
        "promptTokens",
        "promptCacheHitTokens",
        "promptCacheMissTokens",
        "completionTokens",
        "cacheBreakdownComplete",
        "tokenTrackingStartedAt",
        "archivedAt",
        "archivedReason",
    ):
        session.pop(field)
    session.pop("explicitSkillRefs")
    session.pop("skillUsages")
    for run in session.get("runs", []):
        assert isinstance(run, dict)
        run.pop("selectedSkillRefs")
        checkpoint = run.get("checkpoint")
        if isinstance(checkpoint, dict):
            checkpoint.pop("skillRefs")
            checkpoint.pop("automaticSkillIds")
    for request in session.get("pendingRequests", []):
        assert isinstance(request, dict)
        request.pop("skillRefs")
        request.pop("automaticSkillIds")


def test_session_file_has_schema_and_path_safe_deterministic_name(tmp_path: Path) -> None:
    session = make_session("../../越界/session")
    first = JsonSessionStore(tmp_path)
    first.create(session)
    session_file = only_session_file(tmp_path)

    assert session_file.parent == tmp_path / "sessions"
    assert session_file.name not in {"session.json", ".."}
    assert json.loads(session_file.read_text(encoding="utf-8"))["schemaVersion"] == 9
    assert JsonSessionStore(tmp_path).load(session.session_id) == session


def test_v9_tool_message_round_trips_typed_content_and_download_resource(
    tmp_path: Path,
) -> None:
    resource = ToolResourceLink(
        "download",
        "report.md",
        "/downloads/download_123",
        "text/markdown",
        "2026-07-31T00:00:00+00:00",
    )
    message = Message(
        "message_001",
        MessageRole.TOOL,
        "下载快照已创建。",
        NOW,
        1,
        tool_call_id="call_001",
        tool_name="create_download",
        tool_result_status=ToolResultStatus.SUCCESS,
        tool_structured_content={"filename": "report.md", "size_bytes": 12},
        tool_resources=(resource,),
    )
    session = Session("session_001", "test", (message,), "", NOW, NOW, 2)
    store = JsonSessionStore(tmp_path)

    store.create(session)

    document = json.loads(only_session_file(tmp_path).read_text(encoding="utf-8"))
    stored_message = document["session"]["messages"][0]
    assert document["schemaVersion"] == 9
    assert stored_message["tool_structured_content"] == {
        "filename": "report.md",
        "size_bytes": 12,
    }
    assert stored_message["tool_resources"] == [
        {
            "kind": "download",
            "name": "report.md",
            "href": "/downloads/download_123",
            "mime_type": "text/markdown",
            "expires_at": "2026-07-31T00:00:00+00:00",
        }
    ]
    assert store.load(session.session_id) == session


def test_v8_tool_message_defaults_typed_result_fields(tmp_path: Path) -> None:
    message = Message(
        "message_001",
        MessageRole.TOOL,
        "legacy result",
        NOW,
        1,
        tool_call_id="call_001",
        tool_name="read_file",
        tool_result_status=ToolResultStatus.SUCCESS,
    )
    session = Session("session_001", "test", (message,), "", NOW, NOW, 2)
    store = JsonSessionStore(tmp_path)
    store.create(session)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    document["schemaVersion"] = 8
    stored_message = document["session"]["messages"][0]
    stored_message.pop("tool_structured_content")
    stored_message.pop("tool_resources")
    session_file.write_text(json.dumps(document), encoding="utf-8")

    loaded = store.load(session.session_id)

    assert loaded.messages[0].tool_structured_content is None
    assert loaded.messages[0].tool_resources == ()


@pytest.mark.parametrize(
    ("mutate", "error_match"),
    [
        (
            lambda message: message.__setitem__("unexpected", True),
            "invalid fields",
        ),
        (
            lambda message: message.__setitem__("tool_resources", {}),
            "must be an array",
        ),
        (
            lambda message: (
                message.__setitem__("role", "user"),
                message.__setitem__(
                    "tool_resources",
                    [
                        {
                            "kind": "download",
                            "name": "report.md",
                            "href": "/downloads/download_123",
                            "mime_type": None,
                            "expires_at": None,
                        }
                    ],
                ),
            ),
            "only valid for tool messages",
        ),
    ],
)
def test_v9_tool_message_rejects_unknown_fields_wrong_types_and_wrong_roles(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], object],
    error_match: str,
) -> None:
    session = Session(
        "session_001",
        "test",
        (Message("message_001", MessageRole.USER, "hello", NOW, 1),),
        "",
        NOW,
        NOW,
        2,
    )
    store = JsonSessionStore(tmp_path)
    store.create(session)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    stored_message = document["session"]["messages"][0]
    mutate(stored_message)
    session_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StoreError, match=error_match):
        store.load(session.session_id)


def test_v5_session_preserves_skill_fields_and_starts_token_tracking_later(
    tmp_path: Path,
) -> None:
    skill_reference = SkillReference(
        "material-summary",
        f"material-summary-{'a' * 16}",
        "b" * 64,
        "1.0",
    )
    checkpoint = RunCheckpoint(
        "checkpoint_001",
        "run_001",
        "request_001",
        "approval_001",
        0,
        NOW,
        InvocationSource.GATEWAY,
        "gateway",
        "resume request",
        skill_refs=(skill_reference,),
    )
    session = Session(
        "session_001",
        "test",
        (),
        "",
        NOW,
        NOW,
        pending_requests=(
            PendingRequest(
                "request_002",
                1,
                InvocationSource.GATEWAY,
                "gateway",
                "queued request",
                NOW,
                skill_refs=(skill_reference,),
            ),
        ),
        runs=(
            RunState(
                "run_001",
                "request_001",
                RunStatus.STARTED,
                NOW,
                checkpoint=checkpoint,
                selected_skill_refs=(skill_reference,),
            ),
        ),
        explicit_skill_refs=(skill_reference,),
    )
    store = JsonSessionStore(tmp_path)
    store.create(session)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    session_data = document["session"]
    assert isinstance(session_data, dict)
    for field in (
        "promptTokens",
        "promptCacheHitTokens",
        "promptCacheMissTokens",
        "completionTokens",
            "cacheBreakdownComplete",
            "tokenTrackingStartedAt",
            "archivedAt",
            "archivedReason",
        ):
        session_data.pop(field)
    document["schemaVersion"] = 5
    for request in session_data["pendingRequests"]:
        request.pop("automaticSkillIds")
    for run in session_data["runs"]:
        checkpoint_data = run["checkpoint"]
        assert isinstance(checkpoint_data, dict)
        checkpoint_data.pop("automaticSkillIds")
    session_file.write_text(json.dumps(document), encoding="utf-8")

    loaded = store.load(session.session_id)

    assert loaded == session


def test_v8_session_round_trips_automatic_skill_ids(tmp_path: Path) -> None:
    skill_reference = SkillReference(
        "pdf",
        f"pdf-{'a' * 16}",
        "b" * 64,
        "1.0",
    )
    checkpoint = RunCheckpoint(
        "checkpoint_001",
        "run_001",
        "request_001",
        "approval_001",
        0,
        NOW,
        InvocationSource.GATEWAY,
        "gateway",
        "resume request",
        skill_refs=(skill_reference,),
        automatic_skill_ids=("pdf",),
    )
    session = Session(
        "session_001",
        "test",
        (),
        "",
        NOW,
        NOW,
        pending_requests=(
            PendingRequest(
                "request_002",
                1,
                InvocationSource.GATEWAY,
                "gateway",
                "queued request",
                NOW,
                skill_refs=(skill_reference,),
                automatic_skill_ids=("pdf",),
            ),
        ),
        runs=(
            RunState(
                "run_001",
                "request_001",
                RunStatus.STARTED,
                NOW,
                checkpoint=checkpoint,
                selected_skill_refs=(skill_reference,),
            ),
        ),
    )
    store = JsonSessionStore(tmp_path)

    store.create(session)

    document = json.loads(only_session_file(tmp_path).read_text(encoding="utf-8"))
    assert document["session"]["pendingRequests"][0]["automaticSkillIds"] == ["pdf"]
    assert document["session"]["runs"][0]["checkpoint"]["automaticSkillIds"] == ["pdf"]
    assert store.load(session.session_id) == session


def test_legacy_v1_session_loads_and_upgrades_only_on_save(tmp_path: Path) -> None:
    message = Message("message_001", MessageRole.USER, "legacy", NOW, 1)
    session = Session("legacy_session", "legacy", (message,), "", NOW, NOW, 2)
    store = JsonSessionStore(tmp_path)
    store.create(session)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    drop_v5_session_fields(document)
    document["schemaVersion"] = 1
    document["session"].pop("runRecords")
    document["session"].pop("pendingRequests")
    document["session"].pop("runs")
    document["session"].pop("workspaceBinding")
    document["session"].pop("pendingApproval")
    document["session"].pop("approvalDecisions")
    for raw_message in document["session"]["messages"]:
        for field in (
            "toolCalls",
            "reasoningContent",
            "toolName",
            "toolResultStatus",
            "toolErrorCode",
            "attachmentRefs",
            "inputKind",
            "tool_structured_content",
            "tool_resources",
        ):
            raw_message.pop(field)
    session_file.write_text(json.dumps(document), encoding="utf-8")

    assert store.load(session.session_id) == session
    assert json.loads(session_file.read_text(encoding="utf-8"))["schemaVersion"] == 1

    store.save(session)

    assert json.loads(session_file.read_text(encoding="utf-8"))["schemaVersion"] == 9


def test_v2_session_loads_and_upgrades_to_v4_with_pending_requests_and_runs(
    tmp_path: Path,
) -> None:
    store = JsonSessionStore(tmp_path)
    session = Session("session_001", "测试", (), "", NOW, NOW)
    store.create(session)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    drop_v5_session_fields(document)
    document["schemaVersion"] = 2
    document["session"].pop("pendingRequests")
    document["session"].pop("runs")
    document["session"].pop("workspaceBinding")
    document["session"].pop("pendingApproval")
    document["session"].pop("approvalDecisions")
    session_file.write_text(json.dumps(document), encoding="utf-8")

    assert store.load(session.session_id) == session
    assert json.loads(session_file.read_text(encoding="utf-8"))["schemaVersion"] == 2

    upgraded = Session(
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
                InvocationSource.CLI,
                "actor_001",
                "待处理问题",
                NOW,
                ("attachment_001",),
                "idempotency_001",
            ),
        ),
        runs=(
            RunState(
                "run_000",
                "request_000",
                RunStatus.COMPLETED,
                NOW,
                NOW,
                duration_ms=42,
                assistant_message_id="message_001",
            ),
        ),
    )
    store.save(upgraded)

    assert json.loads(session_file.read_text(encoding="utf-8"))["schemaVersion"] == 9
    assert store.load(session.session_id) == upgraded


def test_legacy_v3_run_without_idempotency_key_loads_and_upgrades_on_save(
    tmp_path: Path,
) -> None:
    store = JsonSessionStore(tmp_path)
    session = Session(
        "session_001",
        "测试",
        (),
        "",
        NOW,
        NOW,
        runs=(RunState("run_001", "request_001", RunStatus.QUEUED),),
    )
    store.create(session)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    drop_v5_session_fields(document)
    document["schemaVersion"] = 3
    document["session"].pop("workspaceBinding")
    document["session"].pop("pendingApproval")
    document["session"].pop("approvalDecisions")
    document["session"]["runs"][0].pop("checkpoint")
    document["session"]["runs"][0].pop("idempotencyKey")
    session_file.write_text(json.dumps(document), encoding="utf-8")

    assert store.load(session.session_id) == session
    store.save(session)

    saved = json.loads(session_file.read_text(encoding="utf-8"))
    assert saved["session"]["runs"][0]["idempotencyKey"] is None


def test_v3_message_attachment_refs_load_legacy_and_writer_preserves_them(
    tmp_path: Path,
) -> None:
    store = JsonSessionStore(tmp_path)
    legacy_message = Message(
        "message_001", MessageRole.USER, "问题", NOW, 1
    )
    legacy = Session(
        "session_001", "测试", (legacy_message,), "", NOW, NOW, 2
    )
    store.create(legacy)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    drop_v5_session_fields(document)
    document["schemaVersion"] = 3
    document["session"].pop("workspaceBinding")
    document["session"].pop("pendingApproval")
    document["session"].pop("approvalDecisions")
    document["session"]["messages"][0].pop("attachmentRefs", None)
    document["session"]["messages"][0].pop("inputKind", None)
    document["session"]["messages"][0].pop("tool_structured_content")
    document["session"]["messages"][0].pop("tool_resources")
    session_file.write_text(json.dumps(document), encoding="utf-8")

    assert store.load(legacy.session_id) == legacy
    store.save(legacy)

    saved = json.loads(session_file.read_text(encoding="utf-8"))
    assert saved["session"]["messages"][0]["attachmentRefs"] == []


def test_v3_session_loads_and_upgrades_to_v4_only_after_save(tmp_path: Path) -> None:
    store = JsonSessionStore(tmp_path)
    session = Session(
        "session_001",
        "测试",
        (),
        "",
        NOW,
        NOW,
        runs=(RunState("run_001", "request_001", RunStatus.QUEUED),),
    )
    store.create(session)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    drop_v5_session_fields(document)
    document["schemaVersion"] = 3
    document["session"].pop("workspaceBinding")
    document["session"].pop("pendingApproval")
    document["session"].pop("approvalDecisions")
    document["session"]["runs"][0].pop("checkpoint")
    session_file.write_text(json.dumps(document), encoding="utf-8")

    assert store.load(session.session_id) == session
    assert json.loads(session_file.read_text(encoding="utf-8"))["schemaVersion"] == 3

    store.save(session)

    assert json.loads(session_file.read_text(encoding="utf-8"))["schemaVersion"] == 9


def test_v4_session_round_trips_workspace_approval_and_checkpoint(tmp_path: Path) -> None:
    binding = WorkspaceBinding(
        "workspace_001",
        r"C:\workspace\course",
        "course",
        2,
        NOW,
    )
    tool_call = ToolCall("call_001", "write_file", '{"path":"draft.txt"}')
    intent = ToolIntent(
        "call_001",
        "write_file",
        {"path": "draft.txt"},
        ToolSafetyLevel.WRITE,
        ("draft.txt",),
    )
    pending = PendingApproval(
        "approval_001",
        "run_001",
        "request_001",
        tool_call,
        intent,
        "写入 Workspace 文件",
        NOW,
        NOW + timedelta(minutes=30),
        binding.binding_id,
        binding.revision,
    )
    checkpoint = RunCheckpoint(
        "checkpoint_001",
        "run_001",
        "request_001",
        pending.approval_id,
        1,
        NOW,
        InvocationSource.GATEWAY,
        "gateway",
        "write the draft",
    )
    session = Session(
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
                NOW,
                checkpoint=checkpoint,
            ),
        ),
        workspace_binding=binding,
        pending_approval=pending,
        approval_decisions=(
            ApprovalDecision(
                "decision_001",
                "approval_previous",
                ApprovalDecisionStatus.DENIED,
                "decision-idempotency-001",
                "actor_001",
                NOW,
                "不写这个文件",
            ),
        ),
    )
    store = JsonSessionStore(tmp_path)

    store.create(session)
    document = json.loads(only_session_file(tmp_path).read_text(encoding="utf-8"))
    drop_v5_session_fields(document)
    document["schemaVersion"] = 4
    only_session_file(tmp_path).write_text(json.dumps(document), encoding="utf-8")

    assert store.load(session.session_id) == session
    store.save(session)
    assert json.loads(only_session_file(tmp_path).read_text(encoding="utf-8"))["schemaVersion"] == 9


def test_v4_session_rejects_unknown_workspace_binding_field(tmp_path: Path) -> None:
    session = Session(
        "session_001",
        "测试",
        (),
        "",
        NOW,
        NOW,
        workspace_binding=WorkspaceBinding(
            "workspace_001",
            r"C:\workspace\course",
            "course",
            1,
            NOW,
        ),
    )
    store = JsonSessionStore(tmp_path)
    store.create(session)
    session_file = only_session_file(tmp_path)
    document = json.loads(session_file.read_text(encoding="utf-8"))
    document["session"]["workspaceBinding"]["unexpected"] = True
    session_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StoreError, match="workspaceBinding has invalid fields"):
        store.load(session.session_id)


def test_memory_load_does_not_create_file_until_first_add(tmp_path: Path) -> None:
    store = JsonMemoryStore(tmp_path)

    assert store.load().entries == ()
    assert not (tmp_path / "memory.json").exists()

    store.add(MemoryEntry("memory_001", "记忆", NOW))
    assert (tmp_path / "memory.json").is_file()


def test_v1_memory_loads_lazily_and_replace_upgrades_to_v2(tmp_path: Path) -> None:
    memory_file = tmp_path / "memory.json"
    memory_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "entries": [
                    {
                        "memoryId": "memory_001",
                        "content": "旧记忆",
                        "createdAt": NOW.isoformat(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = JsonMemoryStore(tmp_path)

    legacy = store.load().entries[0]
    assert legacy.category is MemoryCategory.OTHER
    assert legacy.importance == 3
    assert legacy.source_kind is MemorySourceKind.MANUAL
    assert legacy.revision == 1
    assert json.loads(memory_file.read_text(encoding="utf-8"))["schemaVersion"] == 1

    store.replace(Memory((legacy,)))
    assert json.loads(memory_file.read_text(encoding="utf-8"))["schemaVersion"] == 2


def test_v2_memory_rejects_unknown_entry_field(tmp_path: Path) -> None:
    store = JsonMemoryStore(tmp_path)
    store.add(MemoryEntry("memory_001", "内容", NOW))
    memory_file = tmp_path / "memory.json"
    document = json.loads(memory_file.read_text(encoding="utf-8"))
    document["entries"][0]["unexpected"] = True
    memory_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StoreError, match="invalid fields"):
        store.load()


@pytest.mark.parametrize(
    "category",
    (
        MemoryCategory.IDENTITY,
        MemoryCategory.PREFERENCE,
        MemoryCategory.PROJECT,
        MemoryCategory.CONSTRAINT,
        MemoryCategory.EVENT,
        MemoryCategory.OTHER,
    ),
)
def test_v2_memory_round_trips_all_mandated_categories(
    tmp_path: Path,
    category: MemoryCategory,
) -> None:
    entry = MemoryEntry("memory_001", "内容", NOW, category=category)
    store = JsonMemoryStore(tmp_path)

    store.add(entry)

    assert store.load().entries == (entry,)


def test_memory_replace_rejects_corruption_without_overwriting_file(tmp_path: Path) -> None:
    memory_file = tmp_path / "memory.json"
    memory_file.write_text("{broken", encoding="utf-8")
    replacement = MemoryEntry("memory_001", "新内容", NOW)

    with pytest.raises(StoreError, match="invalid JSON"):
        JsonMemoryStore(tmp_path).replace(Memory((replacement,)))

    assert memory_file.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize(
    ("contents", "error_match"),
    [
        ("{broken", "invalid JSON"),
        ('{"schemaVersion": 99, "entries": []}', "schemaVersion"),
        ('{"schemaVersion": 1, "entries": "wrong"}', "entries"),
        ('{"schemaVersion": 1, "entries": [], "extra": true}', "fields"),
        ('{"schemaVersion": 1, "schemaVersion": 1, "entries": []}', "duplicate"),
    ],
)
def test_memory_rejects_corrupt_schema_fields_types_and_duplicate_keys(
    tmp_path: Path,
    contents: str,
    error_match: str,
) -> None:
    memory_file = tmp_path / "memory.json"
    memory_file.write_text(contents, encoding="utf-8")

    with pytest.raises(StoreError, match=error_match):
        JsonMemoryStore(tmp_path).load()


def test_session_rejects_corruption_and_save_does_not_overwrite_it(tmp_path: Path) -> None:
    store = JsonSessionStore(tmp_path)
    session = make_session("session_001")
    store.create(session)
    session_file = only_session_file(tmp_path)
    session_file.write_text("{broken", encoding="utf-8")

    with pytest.raises(StoreError, match="invalid JSON"):
        store.load(session.session_id)
    with pytest.raises(StoreError, match="invalid JSON"):
        store.save(make_session(session.session_id, "不能覆盖"))

    assert session_file.read_text(encoding="utf-8") == "{broken"


def test_list_rejects_duplicate_session_ids_in_files(tmp_path: Path) -> None:
    store = JsonSessionStore(tmp_path)
    store.create(make_session("session_001"))
    original = only_session_file(tmp_path)
    (original.parent / "duplicate.json").write_bytes(original.read_bytes())

    with pytest.raises(StoreError, match="duplicate session_id"):
        store.list()


def test_list_rejects_session_file_with_noncanonical_name(tmp_path: Path) -> None:
    store = JsonSessionStore(tmp_path)
    store.create(make_session("session_001"))
    original = only_session_file(tmp_path)
    original.rename(original.parent / "renamed.json")

    with pytest.raises(StoreError, match="file name does not match"):
        store.list()


def test_atomic_replace_failure_preserves_old_session_and_removes_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonSessionStore(tmp_path)
    store.create(make_session("session_001", "原标题"))
    session_file = only_session_file(tmp_path)
    old_bytes = session_file.read_bytes()

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(StoreError, match="replace failed"):
        store.save(make_session("session_001", "新标题"))

    assert session_file.read_bytes() == old_bytes
    assert not list(session_file.parent.glob("*.tmp"))


def test_atomic_replace_failure_preserves_old_memory_and_removes_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonMemoryStore(tmp_path)
    store.add(MemoryEntry("memory_001", "旧内容", NOW))
    memory_file = tmp_path / "memory.json"
    old_bytes = memory_file.read_bytes()

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(StoreError, match="replace failed"):
        store.add(MemoryEntry("memory_002", "新内容", NOW))

    assert memory_file.read_bytes() == old_bytes
    assert not list(tmp_path.glob("*.tmp"))
