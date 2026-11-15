"""Workspace-bound write, download, and shell tool handlers.

The handlers deliberately obtain workspace facts from the execution context.  They
never use the read-only TOOL_ROOT fallback: a missing binding is a stable tool
failure rather than a weaker sandbox boundary.
"""

import hashlib
import json
import mimetypes
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from sjtuclaw.errors import AttachmentError, ToolExecutionError
from sjtuclaw.infrastructure.tools.context import require_execution_context
from sjtuclaw.infrastructure.tools.readonly import PathGuard
from sjtuclaw.models.attachment import AttachmentStatus
from sjtuclaw.models.tool import (
    JsonValue,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionOutput,
    ToolResourceLink,
    ToolSafetyLevel,
)
from sjtuclaw.ports.attachments import AttachmentStore

_SHA256_PATTERN = "^[0-9a-f]{64}$"
_COPY_ATTACHMENT_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {
        "attachment_id": {"type": "string", "minLength": 1},
        "destination": {"type": "string", "minLength": 1},
        "expected_sha256": {"type": ("string", "null"), "pattern": _SHA256_PATTERN},
        "create_parents": {"type": "boolean", "default": True},
    },
    "required": ("attachment_id", "destination"),
    "additionalProperties": False,
}
_WRITE_FILE_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ("write", "patch")},
        "path": {"type": "string", "minLength": 1},
        "content": {"type": "string"},
        "patch": {"type": "string", "minLength": 1},
        "expected_sha256": {"type": ("string", "null"), "pattern": _SHA256_PATTERN},
        "create_parents": {"type": "boolean", "default": True},
    },
    "required": ("path",),
    "additionalProperties": False,
    "oneOf": (
        {
            "required": ("content",),
            "properties": {"mode": {"const": "write"}},
        },
        {
            "required": ("mode", "patch", "expected_sha256"),
            "properties": {"mode": {"const": "patch"}},
        },
    ),
}
_CREATE_DOWNLOAD_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ("path",),
    "additionalProperties": False,
}
_NEW_SHELL_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {"cwd": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}
_RUN_COMMAND_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "minLength": 1, "maxLength": 16_384},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 900},
    },
    "required": ("command",),
    "additionalProperties": False,
}


def _output_schema(properties: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": tuple(properties),
        "properties": properties,
    }


class AttachmentOriginalReader(Protocol):
    """Session-scoped original-byte source implemented by attachment persistence."""

    def open_original(self, session_id: str, attachment_id: str) -> BinaryIO: ...


@dataclass(frozen=True, slots=True)
class DownloadSnapshot:
    """Safe metadata returned after a download service publishes an immutable snapshot."""

    download_id: str
    filename: str
    size_bytes: int
    sha256: str
    expires_at: str


class DownloadPublisher(Protocol):
    """A service-owned publisher; it creates the snapshot, not the Gateway route."""

    def publish(
        self,
        *,
        session_id: str,
        source: Path,
        filename: str,
    ) -> DownloadSnapshot: ...


@dataclass(frozen=True, slots=True)
class ShellStart:
    cwd: str
    workspace_revision: int
    replaced_existing: bool


@dataclass(frozen=True, slots=True)
class ShellCommand:
    command: str
    cwd_before: str | None
    cwd_after: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool


class ShellManager(Protocol):
    """Volatile shell owner.  This module neither starts nor sandboxes processes."""

    async def new_shell(
        self,
        *,
        context: ToolExecutionContext,
        cwd: Path,
    ) -> ShellStart: ...

    async def run_command(
        self,
        *,
        context: ToolExecutionContext,
        command: str,
        timeout_seconds: int,
    ) -> ShellCommand: ...

    async def close_for_context(self, context: ToolExecutionContext) -> None: ...


@dataclass(frozen=True, slots=True)
class CopyAttachmentToWorkspaceTool:
    attachments: AttachmentStore
    originals: AttachmentOriginalReader

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="copy_attachment_to_workspace",
            description="Copy a ready attachment's original bytes into the active workspace.",
            input_schema=_COPY_ATTACHMENT_SCHEMA,
            output_schema=_output_schema(
                {
                    "attachment_id": {
                        "type": "string",
                        "description": "Opaque attachment ID; not a URL.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Workspace filesystem path; not a URL.",
                    },
                    "size_bytes": {"type": "integer", "minimum": 0},
                    "sha256": {"type": "string", "pattern": _SHA256_PATTERN},
                    "overwritten": {"type": "boolean"},
                }
            ),
            result_usage="Use the metadata to verify the copied workspace file.",
            safety_level=ToolSafetyLevel.WRITE,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        del arguments
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        context, guard = _require_workspace()
        attachment_id = _required_string(arguments, "attachment_id")
        destination = _required_string(arguments, "destination")
        expected_sha256 = _expected_sha256(arguments)
        create_parents = _create_parents(arguments, default=True)
        target = _workspace_target(guard, destination)
        _prepare_parent(guard, target, create_parents)
        _check_destination(target, expected_sha256)
        try:
            attachment = self.attachments.load(context.session_id, attachment_id)
            if attachment.status is not AttachmentStatus.READY:
                raise AttachmentError("ATTACHMENT_NOT_READY")
            source = self.originals.open_original(context.session_id, attachment_id)
        except AttachmentError as error:
            raise ToolExecutionError(error.code) from error
        except OSError as error:
            raise ToolExecutionError("ATTACHMENT_PROCESS_FAILED") from error

        temporary: Path | None = None
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with source, tempfile.NamedTemporaryFile(
                mode="xb", prefix=".sjtuclaw-", dir=target.parent, delete=False
            ) as output:
                temporary = Path(output.name)
                while chunk := source.read(64 * 1024):
                    if not isinstance(chunk, bytes):
                        raise ToolExecutionError("ATTACHMENT_PROCESS_FAILED")
                    digest.update(chunk)
                    size_bytes += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            source_sha256 = digest.hexdigest()
            if source_sha256 != attachment.sha256 or size_bytes != attachment.size_bytes:
                raise ToolExecutionError("ATTACHMENT_CORRUPT")
            _workspace_target(guard, destination)
            overwritten = _check_destination(target, expected_sha256)
            temporary.replace(target)
        except ToolExecutionError:
            raise
        except OSError as error:
            raise ToolExecutionError("WORKSPACE_WRITE_FAILED") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return _execution_output(
            {
                "attachment_id": attachment_id,
                "destination": _relative_result_path(destination),
                "size_bytes": size_bytes,
                "sha256": source_sha256,
                "overwritten": overwritten,
            }
        )


@dataclass(frozen=True, slots=True)
class WriteFileTool:
    """Atomically create or replace one UTF-8 workspace file after a hash check."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_file",
            description="Create or replace one UTF-8 file in the active workspace.",
            input_schema=_WRITE_FILE_SCHEMA,
            output_schema=_output_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "Workspace filesystem path; not a URL.",
                    },
                    "size_bytes": {"type": "integer", "minimum": 0},
                    "sha256": {"type": "string", "pattern": _SHA256_PATTERN},
                    "overwritten": {"type": "boolean"},
                }
            ),
            result_usage="Use the metadata to verify the written workspace file.",
            safety_level=ToolSafetyLevel.WRITE,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        del arguments
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        _, guard = _require_workspace()
        raw_path = _required_string(arguments, "path")
        mode = arguments.get("mode", "write")
        if mode not in {"write", "patch"}:
            raise ToolExecutionError("write mode is invalid")
        expected_sha256 = _expected_sha256(arguments)
        target = _workspace_target(guard, raw_path)
        if mode == "patch" and expected_sha256 is None:
            raise ToolExecutionError("patch requires expected_sha256")
        _prepare_parent(
            guard,
            target,
            False if mode == "patch" else _create_parents(arguments, default=True),
        )
        _check_destination(target, expected_sha256)
        if mode == "write":
            content = _required_string(arguments, "content", allow_empty=True)
        else:
            patch = _required_string(arguments, "patch")
            try:
                content = _apply_unified_patch(
                    target.read_text(encoding="utf-8", errors="strict"),
                    patch,
                )
            except UnicodeDecodeError as error:
                raise ToolExecutionError("WORKSPACE_FILE_NOT_UTF8") from error
        encoded = content.encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="xb", prefix=".sjtuclaw-", dir=target.parent, delete=False
            ) as output:
                temporary = Path(output.name)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            _workspace_target(guard, raw_path)
            overwritten = _check_destination(target, expected_sha256)
            temporary.replace(target)
        except ToolExecutionError:
            raise
        except OSError as error:
            raise ToolExecutionError("WORKSPACE_WRITE_FAILED") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return _execution_output(
            {
                "path": _relative_result_path(raw_path),
                "size_bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "overwritten": overwritten,
            }
        )


@dataclass(frozen=True, slots=True)
class CreateDownloadTool:
    publisher: DownloadPublisher

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="create_download",
            description="Publish an immutable download snapshot for one workspace file.",
            input_schema=_CREATE_DOWNLOAD_SCHEMA,
            output_schema=_output_schema(
                {
                    "filename": {
                        "type": "string",
                        "description": "Display filename metadata only; not a URL.",
                    },
                    "size_bytes": {"type": "integer", "minimum": 0},
                    "sha256": {"type": "string", "pattern": _SHA256_PATTERN},
                    "expires_at": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Expiration timestamp for the host-provided resource."
                        ),
                    },
                }
            ),
            result_usage="Use the host-provided resource action to download the snapshot.",
            safety_level=ToolSafetyLevel.WRITE,
            allowed_resource_kinds=("download",),
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        del arguments
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        context, guard = _require_workspace()
        raw_path = _required_string(arguments, "path")
        source = _workspace_target(guard, raw_path)
        try:
            if not source.is_file():
                raise ToolExecutionError("DOWNLOAD_SOURCE_NOT_FILE")
        except ToolExecutionError:
            raise
        except OSError as error:
            raise ToolExecutionError("WORKSPACE_UNAVAILABLE") from error
        snapshot = self.publisher.publish(
            session_id=context.session_id,
            source=source,
            filename=source.name,
        )
        data: Mapping[str, JsonValue] = {
            "filename": snapshot.filename,
            "size_bytes": snapshot.size_bytes,
            "sha256": snapshot.sha256,
            "expires_at": snapshot.expires_at,
        }
        return ToolExecutionOutput(
            content="下载快照已创建，Host 会显示可用的下载操作；不要构造或猜测下载链接。",
            structured_content=data,
            resources=(
                ToolResourceLink(
                    kind="download",
                    name=snapshot.filename,
                    href=f"/downloads/{snapshot.download_id}",
                    mime_type=mimetypes.guess_type(snapshot.filename)[0]
                    or "application/octet-stream",
                    expires_at=snapshot.expires_at,
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class NewShellTool:
    manager: ShellManager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="new_shell",
            description="Start a non-interactive shell for the active workspace run.",
            input_schema=_NEW_SHELL_SCHEMA,
            output_schema=_output_schema(
                {
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Active shell filesystem directory; not a URL."
                        ),
                    },
                    "workspace_revision": {"type": "integer", "minimum": 1},
                    "replaced_existing": {"type": "boolean"},
                }
            ),
            result_usage="Use cwd and workspace revision as the active shell context.",
            safety_level=ToolSafetyLevel.EXECUTE,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        del arguments
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        context, guard = _require_workspace()
        raw_cwd = _optional_string(arguments, "cwd") or "."
        cwd = (
            guard.resolve(raw_cwd)
            if Path(raw_cwd).is_absolute()
            else _workspace_target(guard, raw_cwd)
        )
        try:
            if not cwd.is_dir():
                raise ToolExecutionError("WORKSPACE_DIRECTORY_NOT_FOUND")
        except ToolExecutionError:
            raise
        except OSError as error:
            raise ToolExecutionError("WORKSPACE_UNAVAILABLE") from error
        started = await self.manager.new_shell(context=context, cwd=cwd)
        return _execution_output(
            {
                "cwd": started.cwd,
                "workspace_revision": started.workspace_revision,
                "replaced_existing": started.replaced_existing,
            }
        )

    async def close_context(self, context: ToolExecutionContext) -> None:
        await self.manager.close_for_context(context)


@dataclass(frozen=True, slots=True)
class RunCommandTool:
    manager: ShellManager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="run_command",
            description="Run one approved command in the active workspace shell.",
            input_schema=_RUN_COMMAND_SCHEMA,
            output_schema=_output_schema(
                {
                    "command": {"type": "string"},
                    "cwd_before": {
                        "type": "string",
                        "description": (
                            "Filesystem directory before execution; not a URL."
                        ),
                    },
                    "cwd_after": {
                        "type": ("string", "null"),
                        "description": (
                            "Filesystem directory after execution, or null when "
                            "unknown; not a URL."
                        ),
                    },
                    "exit_code": {"type": ("integer", "null")},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "timed_out": {
                        "type": "boolean",
                        "description": (
                            "When true, command execution timed out and available "
                            "output may be incomplete."
                        ),
                    },
                    "truncated": {
                        "type": "boolean",
                        "description": (
                            "When true, stdout or stderr is incomplete."
                        ),
                    },
                }
            ),
            result_usage="Use exit status, output, and cwd metadata to assess the command.",
            safety_level=ToolSafetyLevel.EXECUTE,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        del arguments
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        context, _ = _require_workspace()
        command = _required_string(arguments, "command")
        timeout = arguments.get("timeout_seconds", 120)
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise ToolExecutionError("invalid command timeout")
        result = await self.manager.run_command(
            context=context,
            command=command,
            timeout_seconds=timeout,
        )
        return _execution_output(
            {
                "command": result.command,
                "cwd_before": result.cwd_before,
                "cwd_after": result.cwd_after,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
            }
        )


def _require_workspace() -> tuple[ToolExecutionContext, PathGuard]:
    try:
        context = require_execution_context()
    except RuntimeError as error:
        raise ToolExecutionError("WORKSPACE_NOT_CONFIGURED") from error
    if (
        context.workspace_binding_id is None
        or context.workspace_root is None
        or context.workspace_revision is None
    ):
        raise ToolExecutionError("WORKSPACE_NOT_CONFIGURED")
    try:
        return context, PathGuard(context.workspace_root)
    except ValueError as error:
        raise ToolExecutionError("WORKSPACE_UNAVAILABLE") from error


def _workspace_target(guard: PathGuard, raw_path: str) -> Path:
    raw = Path(raw_path)
    if raw.is_absolute():
        raise ToolExecutionError("path must be relative to the active workspace")
    if not raw.parts and raw_path not in {".", "./", ".\\"}:
        raise ToolExecutionError("workspace path is missing")
    _reject_symlink_components(guard.root, raw)
    try:
        return guard.resolve(raw_path)
    except ToolExecutionError:
        raise
    except OSError as error:
        raise ToolExecutionError("path could not be resolved safely") from error


def _reject_symlink_components(root: Path, raw: Path) -> None:
    candidate = root
    for component in raw.parts:
        candidate /= component
        try:
            if candidate.is_symlink():
                raise ToolExecutionError("symbolic links are not allowed")
        except ToolExecutionError:
            raise
        except OSError as error:
            raise ToolExecutionError("path could not be resolved safely") from error


def _prepare_parent(guard: PathGuard, target: Path, create_parents: bool) -> None:
    parent = target.parent
    try:
        if parent.exists():
            if not parent.is_dir():
                raise ToolExecutionError("workspace parent is not a directory")
            return
        if not create_parents:
            raise ToolExecutionError("workspace parent does not exist")
        relative_parent = parent.relative_to(guard.root)
        _workspace_target(guard, str(relative_parent))
        parent.mkdir(parents=True, exist_ok=False)
    except ToolExecutionError:
        raise
    except OSError as error:
        raise ToolExecutionError("WORKSPACE_WRITE_FAILED") from error


def _check_destination(target: Path, expected_sha256: str | None) -> bool:
    try:
        exists = target.exists()
        if not exists:
            if expected_sha256 is not None:
                raise ToolExecutionError("FILE_CHANGED_SINCE_APPROVAL")
            return False
        if target.is_symlink() or not target.is_file():
            raise ToolExecutionError("FILE_CHANGED_SINCE_APPROVAL")
        if expected_sha256 is None or _file_sha256(target) != expected_sha256:
            raise ToolExecutionError("FILE_CHANGED_SINCE_APPROVAL")
        return True
    except ToolExecutionError:
        raise
    except OSError as error:
        raise ToolExecutionError("WORKSPACE_WRITE_FAILED") from error


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(
    arguments: Mapping[str, JsonValue], field: str, *, allow_empty: bool = False
) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ToolExecutionError(f"{field} is missing")
    return value


def _optional_string(arguments: Mapping[str, JsonValue], field: str) -> str | None:
    value = arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolExecutionError(f"{field} is invalid")
    return value


def _expected_sha256(arguments: Mapping[str, JsonValue]) -> str | None:
    value = arguments.get("expected_sha256")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolExecutionError("expected SHA-256 is invalid")
    return value


def _create_parents(
    arguments: Mapping[str, JsonValue],
    *,
    default: bool = False,
) -> bool:
    value = arguments.get("create_parents", default)
    if not isinstance(value, bool):
        raise ToolExecutionError("create_parents is invalid")
    return value


def _relative_result_path(raw_path: str) -> str:
    return Path(raw_path).as_posix()


def _execution_output(value: Mapping[str, JsonValue]) -> ToolExecutionOutput:
    return ToolExecutionOutput(
        content=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        structured_content=value,
    )


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _apply_unified_patch(original: str, patch: str) -> str:
    """Apply one strict unified patch in memory before an atomic write."""

    source = original.splitlines(keepends=True)
    patch_lines = patch.splitlines(keepends=True)
    result: list[str] = []
    source_index = 0
    index = 0
    if index < len(patch_lines) and patch_lines[index].startswith("--- "):
        index += 1
    if index < len(patch_lines) and patch_lines[index].startswith("+++ "):
        index += 1
    saw_hunk = False
    while index < len(patch_lines):
        header = _HUNK_HEADER.match(patch_lines[index].rstrip("\r\n"))
        if header is None:
            raise ToolExecutionError("PATCH_APPLY_FAILED")
        saw_hunk = True
        old_start = int(header.group(1))
        old_count = int(header.group(2) or "1")
        hunk_start = max(old_start - 1, 0)
        if hunk_start < source_index or hunk_start > len(source):
            raise ToolExecutionError("PATCH_APPLY_FAILED")
        result.extend(source[source_index:hunk_start])
        source_index = hunk_start
        consumed = 0
        index += 1
        while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
            line = patch_lines[index]
            if line.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise ToolExecutionError("PATCH_APPLY_FAILED")
            body = line[1:]
            if line[0] == " ":
                if source_index >= len(source) or source[source_index] != body:
                    raise ToolExecutionError("PATCH_APPLY_FAILED")
                result.append(body)
                source_index += 1
                consumed += 1
            elif line[0] == "-":
                if source_index >= len(source) or source[source_index] != body:
                    raise ToolExecutionError("PATCH_APPLY_FAILED")
                source_index += 1
                consumed += 1
            else:
                result.append(body)
            index += 1
        if consumed != old_count:
            raise ToolExecutionError("PATCH_APPLY_FAILED")
    if not saw_hunk:
        raise ToolExecutionError("PATCH_APPLY_FAILED")
    result.extend(source[source_index:])
    return "".join(result)
