"""Advanced workspace tools use trusted context and preserve write integrity."""

import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from sjtuclaw.errors import AttachmentError, ToolExecutionError
from sjtuclaw.infrastructure.tools.advanced import (
    AttachmentOriginalReader,
    CopyAttachmentToWorkspaceTool,
    CreateDownloadTool,
    DownloadSnapshot,
    NewShellTool,
    RunCommandTool,
    ShellCommand,
    ShellStart,
    WriteFileTool,
)
from sjtuclaw.infrastructure.tools.context import reset_execution_context, set_execution_context
from sjtuclaw.infrastructure.tools.registry import ToolRegistry
from sjtuclaw.models.attachment import Attachment, AttachmentKind, AttachmentStatus
from sjtuclaw.models.runtime import InvocationSource
from sjtuclaw.models.tool import (
    AuthorizedToolCall,
    PermissionDecision,
    ToolCall,
    ToolExecutionContext,
    ToolExecutionOutput,
    ToolResult,
    mutable_json,
)
from sjtuclaw.ports.attachments import AttachmentStore

NOW = datetime(2026, 7, 20, tzinfo=UTC)


class FakeAttachments:
    def __init__(self, *, session_id: str, attachment_id: str, content: bytes) -> None:
        self.session_id = session_id
        self.attachment_id = attachment_id
        self.content = content
        self.attachment = Attachment(
            attachment_id=attachment_id,
            session_id=session_id,
            display_name="source.bin",
            kind=AttachmentKind.TEXT,
            status=AttachmentStatus.READY,
            created_at=NOW,
            updated_at=NOW,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            normalized_char_count=0,
        )

    def load(self, session_id: str, attachment_id: str) -> Attachment:
        if session_id != self.session_id or attachment_id != self.attachment_id:
            raise AttachmentError("ATTACHMENT_SESSION_MISMATCH")
        return self.attachment

    def open_original(self, session_id: str, attachment_id: str) -> BinaryIO:
        self.load(session_id, attachment_id)
        return io.BytesIO(self.content)


class RecordingShellManager:
    def __init__(self) -> None:
        self.cwd: Path | None = None

    async def new_shell(
        self,
        *,
        context: ToolExecutionContext,
        cwd: Path,
    ) -> ShellStart:
        self.cwd = cwd
        return ShellStart(
            cwd=str(cwd),
            workspace_revision=context.workspace_revision or 0,
            replaced_existing=False,
        )

    async def run_command(
        self,
        *,
        context: ToolExecutionContext,
        command: str,
        timeout_seconds: int,
    ) -> ShellCommand:
        del context, command, timeout_seconds
        raise AssertionError("run_command is not used by this test")

    async def close_for_context(self, context: ToolExecutionContext) -> None:
        del context


def _context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        "session_001",
        "request_001",
        InvocationSource.CLI,
        workspace_binding_id="binding_001",
        workspace_revision=1,
        workspace_root=root,
    )


async def _execute_with_context(
    tool: (
        CopyAttachmentToWorkspaceTool
        | CreateDownloadTool
        | NewShellTool
        | RunCommandTool
        | WriteFileTool
    ),
    arguments: dict[str, object],
    context: ToolExecutionContext | None,
) -> ToolExecutionOutput:
    token = set_execution_context(context)
    try:
        return await tool.execute(arguments)  # type: ignore[arg-type]
    finally:
        reset_execution_context(token)


def test_new_shell_accepts_dot_for_workspace_root(tmp_path: Path) -> None:
    asyncio.run(_assert_new_shell_accepts_dot_for_workspace_root(tmp_path))


async def _assert_new_shell_accepts_dot_for_workspace_root(tmp_path: Path) -> None:
    manager = RecordingShellManager()
    result = await _execute_with_context(
        NewShellTool(manager=manager),
        {"cwd": "."},
        _context(tmp_path),
    )

    assert manager.cwd == tmp_path.resolve()
    expected = {
        "cwd": str(tmp_path.resolve()),
        "workspace_revision": 1,
        "replaced_existing": False,
    }
    assert json.loads(result.content) == expected
    assert mutable_json(result.structured_content) == expected
    assert result.resources == ()


def test_copy_attachment_preserves_original_bytes_and_hides_paths(tmp_path: Path) -> None:
    asyncio.run(_assert_copy_attachment_preserves_original_bytes_and_hides_paths(tmp_path))


def test_copy_attachment_creates_parent_directory_by_default(tmp_path: Path) -> None:
    asyncio.run(_assert_copy_attachment_creates_parent_directory_by_default(tmp_path))


async def _assert_copy_attachment_creates_parent_directory_by_default(
    tmp_path: Path,
) -> None:
    content = b"attachment"
    fake = FakeAttachments(
        session_id="session_001",
        attachment_id="attachment_001",
        content=content,
    )
    tool = CopyAttachmentToWorkspaceTool(
        attachments=cast(AttachmentStore, fake),
        originals=cast(AttachmentOriginalReader, fake),
    )

    await _execute_with_context(
        tool,
        {
            "attachment_id": "attachment_001",
            "destination": "inputs/source.bin",
        },
        _context(tmp_path),
    )

    assert (tmp_path / "inputs" / "source.bin").read_bytes() == content


async def _assert_copy_attachment_preserves_original_bytes_and_hides_paths(tmp_path: Path) -> None:
    content = b"\x00raw\r\nbytes\xff"
    fake = FakeAttachments(
        session_id="session_001",
        attachment_id="attachment_001",
        content=content,
    )
    tool = CopyAttachmentToWorkspaceTool(
        attachments=cast(AttachmentStore, fake),
        originals=cast(AttachmentOriginalReader, fake),
    )

    result = await _execute_with_context(
        tool,
        {
            "attachment_id": "attachment_001",
            "destination": "materials/source.bin",
            "create_parents": True,
        },
        _context(tmp_path),
    )

    assert isinstance(result, ToolExecutionOutput)
    assert (tmp_path / "materials" / "source.bin").read_bytes() == content
    assert json.loads(result.content) == {
        "attachment_id": "attachment_001",
        "destination": "materials/source.bin",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "overwritten": False,
    }
    assert mutable_json(result.structured_content) == {
        "attachment_id": "attachment_001",
        "destination": "materials/source.bin",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "overwritten": False,
    }
    assert result.resources == ()
    assert str(tmp_path) not in result.content


def test_write_file_rejects_stale_hash_without_overwriting(tmp_path: Path) -> None:
    asyncio.run(_assert_write_file_rejects_stale_hash_without_overwriting(tmp_path))


def test_write_file_creates_parent_directory_by_default(tmp_path: Path) -> None:
    asyncio.run(_assert_write_file_creates_parent_directory_by_default(tmp_path))


async def _assert_write_file_creates_parent_directory_by_default(tmp_path: Path) -> None:
    await _execute_with_context(
        WriteFileTool(),
        {
            "path": "inputs/mindmap.md",
            "content": "# Mind map",
        },
        _context(tmp_path),
    )

    assert (tmp_path / "inputs" / "mindmap.md").read_text(encoding="utf-8") == "# Mind map"


async def _assert_write_file_rejects_stale_hash_without_overwriting(tmp_path: Path) -> None:
    destination = tmp_path / "notes.txt"
    destination.write_text("current", encoding="utf-8")
    tool = WriteFileTool()

    with pytest.raises(ToolExecutionError, match="FILE_CHANGED_SINCE_APPROVAL"):
        await _execute_with_context(
            tool,
            {
                "path": "notes.txt",
                "content": "replacement",
                "expected_sha256": hashlib.sha256(b"stale").hexdigest(),
            },
            _context(tmp_path),
        )

    assert destination.read_text(encoding="utf-8") == "current"


def test_advanced_write_requires_a_trusted_workspace_binding(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ToolExecutionError, match="WORKSPACE_NOT_CONFIGURED"):
        asyncio.run(_execute_with_context(WriteFileTool(), {"path": "notes.txt", "content": "x"}, None))


def test_advanced_write_rejects_workspace_escape(tmp_path: Path) -> None:
    asyncio.run(_assert_advanced_write_rejects_workspace_escape(tmp_path))


async def _assert_advanced_write_rejects_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="outside the allowed root"):
        await _execute_with_context(
            WriteFileTool(),
            {"path": "../escape.txt", "content": "x"},
            _context(tmp_path),
        )
    assert not (tmp_path.parent / "escape.txt").exists()


class FakeDownloadPublisher:
    def __init__(self, snapshot: DownloadSnapshot) -> None:
        self.snapshot = snapshot

    def publish(
        self,
        *,
        session_id: str,
        source: Path,
        filename: str,
    ) -> DownloadSnapshot:
        assert session_id == "session_001"
        assert source.name == "report.txt"
        assert filename == "report.txt"
        return self.snapshot


def test_create_download_returns_host_resource_without_exposing_download_id(
    tmp_path: Path,
) -> None:
    async def execute() -> ToolExecutionOutput:
        (tmp_path / "report.txt").write_text("report", encoding="utf-8")
        snapshot = DownloadSnapshot(
            download_id="download_001",
            filename="report.txt",
            size_bytes=6,
            sha256=hashlib.sha256(b"report").hexdigest(),
            expires_at="2026-07-20T01:00:00+00:00",
        )
        result = await _execute_with_context(
            CreateDownloadTool(FakeDownloadPublisher(snapshot)),
            {"path": "report.txt"},
            _context(tmp_path),
        )
        assert isinstance(result, ToolExecutionOutput)
        return result

    output = asyncio.run(execute())

    assert "download_001" not in output.content
    assert mutable_json(output.structured_content) == {
        "filename": "report.txt",
        "size_bytes": 6,
        "sha256": hashlib.sha256(b"report").hexdigest(),
        "expires_at": "2026-07-20T01:00:00+00:00",
    }
    assert output.resources == (
        output.resources[0],
    )
    resource = output.resources[0]
    assert resource.kind == "download"
    assert resource.name == "report.txt"
    assert resource.href == "/downloads/download_001"
    assert resource.mime_type == "text/plain"
    assert resource.expires_at == "2026-07-20T01:00:00+00:00"


def test_write_file_returns_compact_content_and_structured_metadata(tmp_path: Path) -> None:
    output = asyncio.run(
        _execute_with_context(
            WriteFileTool(),
            {"path": "notes.txt", "content": "hello"},
            _context(tmp_path),
        )
    )
    expected = {
        "path": "notes.txt",
        "size_bytes": 5,
        "sha256": hashlib.sha256(b"hello").hexdigest(),
        "overwritten": False,
    }

    assert json.loads(output.content) == expected
    assert mutable_json(output.structured_content) == expected
    assert output.resources == ()


class FakeShellManager:
    async def new_shell(
        self,
        *,
        context: ToolExecutionContext,
        cwd: Path,
    ) -> ShellStart:
        assert context.workspace_revision == 1
        return ShellStart(cwd.as_posix(), 1, False)

    async def run_command(
        self,
        *,
        context: ToolExecutionContext,
        command: str,
        timeout_seconds: int,
    ) -> ShellCommand:
        assert context.workspace_revision == 1
        assert timeout_seconds == 120
        return ShellCommand(
            command=command,
            cwd_before="workspace",
            cwd_after="workspace",
            exit_code=0,
            stdout="done\n",
            stderr="",
            timed_out=False,
            truncated=False,
        )

    async def close_for_context(self, context: ToolExecutionContext) -> None:
        del context


class TimeoutShellManager(FakeShellManager):
    async def run_command(
        self,
        *,
        context: ToolExecutionContext,
        command: str,
        timeout_seconds: int,
    ) -> ShellCommand:
        assert context.workspace_revision == 1
        assert timeout_seconds == 120
        return ShellCommand(
            command=command,
            cwd_before="workspace",
            cwd_after=None,
            exit_code=None,
            stdout="partial output",
            stderr="",
            timed_out=True,
            truncated=False,
        )


def test_run_command_timeout_preserves_unknown_cwd_and_diagnostics(
    tmp_path: Path,
) -> None:
    async def execute() -> ToolResult:
        registry = ToolRegistry((RunCommandTool(TimeoutShellManager()),))
        intent = registry.validate_batch(
            (ToolCall("call-1", "run_command", '{"command":"slow command"}'),)
        )[0]
        authorized = AuthorizedToolCall.from_permission(
            intent,
            PermissionDecision.ALLOW,
        )
        return (await registry.execute_batch((authorized,), context=_context(tmp_path)))[0]

    result = asyncio.run(execute())
    expected = {
        "command": "slow command",
        "cwd_before": "workspace",
        "cwd_after": None,
        "exit_code": None,
        "stdout": "partial output",
        "stderr": "",
        "timed_out": True,
        "truncated": False,
    }

    assert result.is_error is False
    assert json.loads(result.content) == expected
    assert mutable_json(result.structured_content) == expected
    assert result.resources == ()


def test_shell_tools_return_compact_content_and_structured_metadata(tmp_path: Path) -> None:
    async def execute() -> tuple[ToolExecutionOutput, ToolExecutionOutput]:
        manager = FakeShellManager()
        (tmp_path / "work").mkdir()
        shell = await _execute_with_context(
            NewShellTool(manager),
            {"cwd": "work"},
            _context(tmp_path),
        )
        command = await _execute_with_context(
            RunCommandTool(manager),
            {"command": "echo done"},
            _context(tmp_path),
        )
        return shell, command

    shell, command = asyncio.run(execute())
    expected_shell = {
        "cwd": (tmp_path / "work").as_posix(),
        "workspace_revision": 1,
        "replaced_existing": False,
    }
    expected_command = {
        "command": "echo done",
        "cwd_before": "workspace",
        "cwd_after": "workspace",
        "exit_code": 0,
        "stdout": "done\n",
        "stderr": "",
        "timed_out": False,
        "truncated": False,
    }

    assert json.loads(shell.content) == expected_shell
    assert mutable_json(shell.structured_content) == expected_shell
    assert shell.resources == ()
    assert json.loads(command.content) == expected_command
    assert mutable_json(command.structured_content) == expected_command
    assert command.resources == ()
