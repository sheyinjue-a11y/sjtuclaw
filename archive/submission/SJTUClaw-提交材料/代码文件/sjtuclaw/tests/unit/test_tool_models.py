"""Tool domain contracts remain immutable and provider independent."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from sjtuclaw.infrastructure.tools.advanced import (
    AttachmentOriginalReader,
    CopyAttachmentToWorkspaceTool,
    CreateDownloadTool,
    DownloadPublisher,
    NewShellTool,
    RunCommandTool,
    ShellManager,
    WriteFileTool,
)
from sjtuclaw.infrastructure.tools.readonly import (
    CurrentTimeTool,
    ListDirTool,
    PathGuard,
    ReadAttachmentTool,
    ReadFileTool,
)
from sjtuclaw.infrastructure.tools.skill_selection import SelectSkillTool
from sjtuclaw.infrastructure.tools.skills import ReadSkillResourceTool
from sjtuclaw.models.tool import (
    AuthorizedToolCall,
    JsonValue,
    PendingApproval,
    PermissionDecision,
    ToolCall,
    ToolDefinition,
    ToolExecutionOutput,
    ToolIntent,
    ToolResourceLink,
    ToolResult,
    ToolResultStatus,
    ToolSafetyLevel,
    mutable_json,
)
from sjtuclaw.ports.attachments import AttachmentStore
from sjtuclaw.runtime.skill_service import SkillService


def test_execution_output_deep_freezes_structured_content() -> None:
    resource = ToolResourceLink(
        kind="download",
        name="report.md",
        href="/downloads/download_123",
        mime_type="text/markdown",
        expires_at="2026-07-31T00:00:00+00:00",
    )
    structured_content: dict[str, JsonValue] = {
        "filename": "report.md",
        "size_bytes": 12,
    }

    output = ToolExecutionOutput(
        content="下载快照已创建。",
        structured_content=structured_content,
        resources=(resource,),
    )
    structured_content["filename"] = "changed.md"

    assert output.content == "下载快照已创建。"
    assert output.structured_content["filename"] == "report.md"
    assert output.resources == (resource,)
    with pytest.raises(TypeError):
        output.structured_content["filename"] = "cannot mutate"  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", ""),
        ("name", ""),
        ("href", "https://example.test/downloads/download_123"),
        ("href", "downloads/download_123"),
        ("href", "//example.test/downloads/download_123"),
    ],
)
def test_resource_link_rejects_invalid_identity_or_href(
    field: str,
    value: str,
) -> None:
    values = {
        "kind": "download",
        "name": "report.md",
        "href": "/downloads/download_123",
    }
    values[field] = value

    with pytest.raises(ValueError):
        ToolResourceLink(**values)


def test_resource_link_rejects_backslash_host_escape() -> None:
    with pytest.raises(ValueError):
        ToolResourceLink(
            kind="download",
            name="report.md",
            href="/\\evil.example/download",
        )


def test_resource_link_and_execution_output_require_text_fields() -> None:
    with pytest.raises(TypeError):
        ToolResourceLink(kind=1, name="report.md", href="/downloads/download_123")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ToolExecutionOutput(
            content=cast(str, 1),
            structured_content={},
        )


def test_successful_result_preserves_structured_content_and_resources() -> None:
    resource = ToolResourceLink(
        kind="download",
        name="report.md",
        href="/downloads/download_123",
        mime_type="text/markdown",
        expires_at="2026-07-31T00:00:00+00:00",
    )
    structured_content: dict[str, JsonValue] = {
        "filename": "report.md",
        "size_bytes": 12,
    }

    result = ToolResult(
        call_id="call-1",
        tool_name="create_download",
        content="下载快照已创建。",
        structured_content=structured_content,
        resources=(resource,),
    )
    structured_content["filename"] = "changed.md"

    assert result.structured_content is not None
    assert result.structured_content["filename"] == "report.md"
    assert result.resources == (resource,)
    with pytest.raises(TypeError):
        result.structured_content["filename"] = "cannot mutate"  # type: ignore[index]


def test_error_result_rejects_resources() -> None:
    resource = ToolResourceLink(
        kind="download",
        name="report.md",
        href="/downloads/download_123",
    )

    with pytest.raises(ValueError, match="resources"):
        ToolResult(
            call_id="call-1",
            tool_name="create_download",
            content="download failed",
            status=ToolResultStatus.ERROR,
            error_code="DOWNLOAD_FAILED",
            resources=(resource,),
        )


class _FalsyResourceIterable:
    def __init__(self, resource: ToolResourceLink) -> None:
        self.resource = resource

    def __bool__(self) -> bool:
        return False

    def __iter__(self) -> Iterator[ToolResourceLink]:
        yield self.resource


def test_error_result_rejects_resources_from_falsy_iterable() -> None:
    resource = ToolResourceLink(
        kind="download",
        name="report.md",
        href="/downloads/download_123",
    )

    with pytest.raises(ValueError, match="resources"):
        ToolResult(
            call_id="call-1",
            tool_name="create_download",
            content="download failed",
            status=ToolResultStatus.ERROR,
            error_code="DOWNLOAD_FAILED",
            resources=cast(tuple[ToolResourceLink, ...], _FalsyResourceIterable(resource)),
        )


def test_tool_call_keeps_malformed_provider_arguments_for_batch_validation() -> None:
    call = ToolCall(call_id="call-1", name="read_file", arguments_json="{broken")

    assert call.arguments_json == "{broken"


def test_tool_definition_and_intent_deep_freeze_mappings() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }
    arguments: dict[str, JsonValue] = {
        "path": "README.md",
        "nested": {"items": (1, 2)},
    }
    definition = ToolDefinition(
        name="read_file",
        description="Read a UTF-8 file",
        input_schema=schema,
        output_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        result_usage="Use the file content.",
        safety_level=ToolSafetyLevel.READ_ONLY,
    )
    intent = ToolIntent(
        call_id="call-1",
        name="read_file",
        arguments=arguments,
        safety_level=ToolSafetyLevel.READ_ONLY,
        target_resources=(str(Path("README.md")),),
    )

    schema["type"] = "array"
    arguments["path"] = "changed"
    nested = intent.arguments["nested"]

    assert definition.input_schema["type"] == "object"
    assert definition.effect is ToolSafetyLevel.READ_ONLY
    assert intent.arguments["path"] == "README.md"
    assert intent.effect is ToolSafetyLevel.READ_ONLY
    assert isinstance(nested, dict) is False
    with pytest.raises(TypeError):
        intent.arguments["path"] = "cannot mutate"  # type: ignore[index]


def test_builtin_output_schemas_explain_high_risk_values(tmp_path: Path) -> None:
    guard = PathGuard(tmp_path)
    shell_manager = cast(ShellManager, None)
    skill_service = cast(SkillService, None)
    definitions = (
        CurrentTimeTool(lambda: datetime(2026, 7, 30, tzinfo=UTC)).definition,
        ListDirTool(guard).definition,
        ReadFileTool(guard, 1024).definition,
        ReadAttachmentTool(cast(AttachmentStore, None)).definition,
        CopyAttachmentToWorkspaceTool(
            attachments=cast(AttachmentStore, None),
            originals=cast(AttachmentOriginalReader, None),
        ).definition,
        WriteFileTool().definition,
        CreateDownloadTool(cast(DownloadPublisher, None)).definition,
        NewShellTool(shell_manager).definition,
        RunCommandTool(shell_manager).definition,
        ReadSkillResourceTool(skills=skill_service).definition,
        SelectSkillTool(skills=skill_service).definition,
    )
    required_semantics: dict[str, dict[str, tuple[str, ...]]] = {
        "current_time": {"iso8601": ("timezone-aware",)},
        "list_dir": {
            "path": ("filesystem", "not a url"),
            "entries": ("filesystem", "do not authorize"),
        },
        "read_file": {"path": ("filesystem", "not a url")},
        "read_attachment": {
            "attachment_id": ("opaque", "not a url"),
            "truncated": ("true", "incomplete"),
            "next_cursor": ("continuing", "only", "not a url"),
        },
        "copy_attachment_to_workspace": {
            "attachment_id": ("opaque", "not a url"),
            "destination": ("filesystem", "not a url"),
        },
        "write_file": {"path": ("filesystem", "not a url")},
        "create_download": {
            "filename": ("metadata", "not a url"),
            "expires_at": ("expiration",),
        },
        "new_shell": {"cwd": ("filesystem", "not a url")},
        "run_command": {
            "cwd_before": ("filesystem", "not a url"),
            "cwd_after": ("filesystem", "not a url"),
            "timed_out": ("true", "incomplete"),
            "truncated": ("true", "incomplete"),
        },
        "read_skill_resource": {
            "skill_id": ("opaque", "not a url"),
            "path": ("resource path", "not a url"),
            "truncated": ("true", "incomplete"),
            "next_cursor": ("continuing", "only", "not a url"),
        },
        "select_skill": {
            "skill_id": ("opaque", "not a url"),
            "snapshot_id": ("opaque", "not a url"),
        },
    }

    assert {definition.name for definition in definitions} == set(required_semantics)
    for definition in definitions:
        output_schema = mutable_json(definition.output_schema)
        assert isinstance(output_schema, dict)
        properties = output_schema["properties"]
        assert isinstance(properties, dict)
        for field, semantics in required_semantics[definition.name].items():
            property_schema = properties[field]
            assert isinstance(property_schema, dict)
            description = property_schema.get("description")
            assert isinstance(description, str) and description.strip()
            normalized = description.casefold()
            assert all(term in normalized for term in semantics)

        if definition.name == "list_dir":
            entries = properties["entries"]
            assert isinstance(entries, dict)
            items = entries["items"]
            assert isinstance(items, dict)
            entry_properties = items["properties"]
            assert isinstance(entry_properties, dict)
            name_schema = entry_properties["name"]
            assert isinstance(name_schema, dict)
            name_description = name_schema.get("description")
            assert isinstance(name_description, str) and name_description.strip()
            assert "filesystem entry name" in name_description.casefold()
            assert "not a url" in name_description.casefold()


def test_authorized_call_can_only_be_created_from_allow_decision() -> None:
    intent = ToolIntent(
        call_id="call-1",
        name="current_time",
        arguments={},
        safety_level=ToolSafetyLevel.READ_ONLY,
    )

    authorized = AuthorizedToolCall.from_permission(intent, PermissionDecision.ALLOW)

    assert authorized.intent == intent
    with pytest.raises(ValueError, match="allow"):
        AuthorizedToolCall.from_permission(intent, PermissionDecision.DENY)
    with pytest.raises(TypeError, match="PermissionPolicy"):
        AuthorizedToolCall(intent)


def test_result_and_pending_approval_have_stable_correlation_fields() -> None:
    intent = ToolIntent(
        call_id="call-1",
        name="future_write",
        arguments={"path": "notes.txt"},
        safety_level=ToolSafetyLevel.WRITE,
        target_resources=("notes.txt",),
    )
    pending = PendingApproval(
        approval_id="approval-1",
        intent=intent,
        reason="write requires approval",
    )
    result = ToolResult(
        call_id="call-1",
        tool_name="future_write",
        content="permission denied",
        status=ToolResultStatus.ERROR,
        error_code="PERMISSION_DENIED",
    )

    assert pending.intent.call_id == result.call_id
    assert result.is_error is True
