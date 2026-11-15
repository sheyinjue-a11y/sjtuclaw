"""Sandboxed built-in read-only tools."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sjtuclaw.errors import AttachmentError, ToolExecutionError
from sjtuclaw.infrastructure.tools.context import require_execution_context
from sjtuclaw.models.tool import (
    JsonValue,
    ToolDefinition,
    ToolExecutionOutput,
    ToolSafetyLevel,
)
from sjtuclaw.ports.attachments import AttachmentStore

_NO_ARGUMENTS_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_PATH_ARGUMENT_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ("path",),
    "additionalProperties": False,
}
_READ_ATTACHMENT_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {
        "attachment_id": {"type": "string", "minLength": 1},
        "cursor": {"type": ("string", "null")},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 32_000},
    },
    "required": ("attachment_id",),
    "additionalProperties": False,
}


def _output_schema(properties: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": tuple(properties),
        "properties": properties,
    }


class PathGuard:
    """Resolve paths inside one root while blocking sensitive components."""

    def __init__(self, root: Path) -> None:
        resolved = root.resolve(strict=False)
        if not resolved.is_dir():
            raise ValueError("tool root must be an existing directory")
        self.root = resolved

    def unresolved_target(self, raw_path: str) -> str:
        path = Path(raw_path)
        target = path if path.is_absolute() else self.root / path
        return str(target)

    def resolve(self, raw_path: str) -> Path:
        raw = Path(raw_path)
        if _has_sensitive_component(raw):
            raise ToolExecutionError("access to sensitive paths is denied")
        candidate = raw if raw.is_absolute() else self.root / raw
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as error:
            raise ToolExecutionError("path could not be resolved safely") from error
        if not resolved.is_relative_to(self.root):
            raise ToolExecutionError("path is outside the allowed root")
        if _has_sensitive_component(resolved.relative_to(self.root)):
            raise ToolExecutionError("access to sensitive paths is denied")
        return resolved

    def is_visible_child(self, child: Path) -> bool:
        try:
            if _has_sensitive_component(Path(child.name)):
                return False
            resolved = child.resolve(strict=False)
            return resolved.is_relative_to(self.root) and not _has_sensitive_component(
                resolved.relative_to(self.root)
            )
        except OSError:
            return False


@dataclass(frozen=True, slots=True)
class CurrentTimeTool:
    now: Callable[[], datetime]

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="current_time",
            description="Return the current timezone-aware time in ISO 8601 format.",
            input_schema=_NO_ARGUMENTS_SCHEMA,
            output_schema=_output_schema(
                {
                    "iso8601": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Timezone-aware timestamp in ISO 8601 format.",
                    }
                }
            ),
            result_usage="Use iso8601 as the current timezone-aware timestamp.",
            safety_level=ToolSafetyLevel.READ_ONLY,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        current = self.now()
        if current.tzinfo is None:
            raise ToolExecutionError("current time source returned a timezone-naive value")
        iso8601 = current.isoformat()
        return ToolExecutionOutput(
            content=iso8601,
            structured_content={"iso8601": iso8601},
        )


@dataclass(frozen=True, slots=True)
class ListDirTool:
    guard: PathGuard

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_dir",
            description="List safe entries in a directory under the configured root.",
            input_schema=_PATH_ARGUMENT_SCHEMA,
            output_schema=_output_schema(
                {
                    "path": {
                        "type": "string",
                        "description": (
                            "Filesystem directory path under the configured root; "
                            "not a URL."
                        ),
                    },
                    "entries": {
                        "type": "array",
                        "description": (
                            "Filesystem entries within the listed directory; names "
                            "do not authorize access outside it."
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ("name", "type"),
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": (
                                        "Filesystem entry name relative to the listed "
                                        "directory; not a URL."
                                    ),
                                },
                                "type": {
                                    "type": "string",
                                    "enum": ("file", "directory"),
                                },
                            },
                        },
                    },
                }
            ),
            result_usage="Use entries as the directory's safe file and directory listing.",
            safety_level=ToolSafetyLevel.READ_ONLY,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        return (self.guard.unresolved_target(_path_argument(arguments)),)

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        guard = _guard_for_execution(self.guard)
        raw_path = _path_argument(arguments)
        path = guard.resolve(raw_path)
        try:
            if not path.exists():
                raise ToolExecutionError("directory does not exist")
            if not path.is_dir():
                raise ToolExecutionError("requested path is not a directory")
            entries = tuple(path.iterdir())
        except ToolExecutionError:
            raise
        except OSError as error:
            raise ToolExecutionError("directory could not be read") from error

        visible = [entry for entry in entries if guard.is_visible_child(entry)]
        visible.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        rendered: list[str] = []
        structured_entries: list[Mapping[str, JsonValue]] = []
        for entry in visible:
            try:
                is_directory = entry.is_dir()
            except OSError:
                is_directory = False
            suffix = "/" if is_directory else ""
            rendered.append(f"{entry.name}{suffix}")
            structured_entries.append(
                {
                    "name": entry.name,
                    "type": "directory" if is_directory else "file",
                }
            )
        return ToolExecutionOutput(
            content="\n".join(rendered),
            structured_content={
                "path": Path(raw_path).as_posix(),
                "entries": tuple(structured_entries),
            },
        )


@dataclass(frozen=True, slots=True)
class ReadFileTool:
    guard: PathGuard
    max_file_bytes: int

    def __post_init__(self) -> None:
        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            description="Read a bounded UTF-8 text file under the configured root.",
            input_schema=_PATH_ARGUMENT_SCHEMA,
            output_schema=_output_schema(
                {
                    "path": {
                        "type": "string",
                        "description": (
                            "Filesystem path under the configured root; not a URL."
                        ),
                    },
                    "size_bytes": {"type": "integer", "minimum": 0},
                }
            ),
            result_usage="Use path and size_bytes as metadata for the text in content.",
            safety_level=ToolSafetyLevel.READ_ONLY,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        return (self.guard.unresolved_target(_path_argument(arguments)),)

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        guard = _guard_for_execution(self.guard)
        raw_path = _path_argument(arguments)
        path = guard.resolve(raw_path)
        try:
            if not path.exists():
                raise ToolExecutionError("file does not exist")
            if not path.is_file():
                raise ToolExecutionError("requested path is not a file")
            with path.open("rb") as stream:
                content = stream.read(self.max_file_bytes + 1)
        except ToolExecutionError:
            raise
        except OSError as error:
            raise ToolExecutionError("file could not be read") from error
        if len(content) > self.max_file_bytes:
            raise ToolExecutionError("file exceeds the configured size limit")
        try:
            decoded = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ToolExecutionError("file is not valid UTF-8 text") from error
        return ToolExecutionOutput(
            content=decoded,
            structured_content={
                "path": Path(raw_path).as_posix(),
                "size_bytes": len(content),
            },
        )


@dataclass(frozen=True, slots=True)
class ReadAttachmentTool:
    """Read only a bounded normalized segment from the current Session."""

    attachments: AttachmentStore
    max_chars: int = 32_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_chars <= 32_000:
            raise ValueError("max_chars must be between 1 and 32000")

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_attachment",
            description="Read a bounded text segment from an attachment selected in this Session.",
            input_schema=_READ_ATTACHMENT_SCHEMA,
            output_schema=_output_schema(
                {
                    "attachment_id": {
                        "type": "string",
                        "description": "Opaque attachment ID; not a URL.",
                    },
                    "truncated": {
                        "type": "boolean",
                        "description": (
                            "When true, content is incomplete and must be continued."
                        ),
                    },
                    "next_cursor": {
                        "type": ("string", "null"),
                        "description": (
                            "Opaque cursor used only for continuing this read; "
                            "not a URL."
                        ),
                    },
                }
            ),
            result_usage="Use attachment metadata to continue reading content when truncated.",
            safety_level=ToolSafetyLevel.READ_ONLY,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        del arguments
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        attachment_id = arguments.get("attachment_id")
        cursor = arguments.get("cursor")
        requested_max = arguments.get("max_chars", self.max_chars)
        if not isinstance(attachment_id, str) or not attachment_id:
            raise ToolExecutionError("attachment id is missing")
        if cursor is not None and not isinstance(cursor, str):
            raise ToolExecutionError("attachment cursor is invalid")
        if not isinstance(requested_max, int) or isinstance(requested_max, bool):
            raise ToolExecutionError("attachment read limit is invalid")
        try:
            context = require_execution_context()
        except RuntimeError as error:
            raise ToolExecutionError("attachment context is unavailable") from error
        try:
            segment = self.attachments.read_slice(
                context.session_id,
                attachment_id,
                cursor=cursor,
                max_chars=min(requested_max, self.max_chars),
            )
        except AttachmentError as error:
            raise ToolExecutionError(error.code) from error
        content = json.dumps(
            {"content": segment.content, "next_cursor": segment.next_cursor},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return ToolExecutionOutput(
            content=content,
            structured_content={
                "attachment_id": attachment_id,
                "truncated": segment.next_cursor is not None,
                "next_cursor": segment.next_cursor,
            },
        )


def create_read_only_tools(
    root: Path,
    *,
    now: Callable[[], datetime] | None = None,
    max_file_bytes: int = 1_048_576,
    attachments: AttachmentStore | None = None,
) -> tuple[CurrentTimeTool | ListDirTool | ReadAttachmentTool | ReadFileTool, ...]:
    guard = PathGuard(root)
    clock = now if now is not None else lambda: datetime.now(UTC)
    tools: tuple[CurrentTimeTool | ListDirTool | ReadFileTool, ...] = (
        CurrentTimeTool(clock),
        ListDirTool(guard),
        ReadFileTool(guard, max_file_bytes),
    )
    if attachments is None:
        return tools
    return (*tools, ReadAttachmentTool(attachments))


def _path_argument(arguments: Mapping[str, JsonValue]) -> str:
    value = arguments.get("path")
    if not isinstance(value, str):
        raise ToolExecutionError("validated path argument is missing")
    if Path(value).is_absolute():
        raise ToolExecutionError("path must be relative to the active root")
    return value


def _guard_for_execution(default: PathGuard) -> PathGuard:
    """Resolve Workspace roots fresh for every tool call, never as a fallback."""

    try:
        context = require_execution_context()
    except RuntimeError:
        return default
    if context.workspace_binding_id is None:
        return default
    if context.workspace_root is None or context.workspace_revision is None:
        raise ToolExecutionError("WORKSPACE_UNAVAILABLE")
    try:
        return PathGuard(context.workspace_root)
    except ValueError as error:
        raise ToolExecutionError("WORKSPACE_UNAVAILABLE") from error


def _has_sensitive_component(path: Path) -> bool:
    for part in path.parts:
        name = part.casefold()
        if name == ".git":
            return True
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return True
    return False
