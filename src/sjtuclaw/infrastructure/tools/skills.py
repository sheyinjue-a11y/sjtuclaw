"""Read-only bounded access to resources in already selected Skill snapshots."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping

from sjtuclaw.errors import SkillError, ToolExecutionError
from sjtuclaw.infrastructure.tools.context import require_execution_context
from sjtuclaw.models.tool import (
    JsonValue,
    ToolDefinition,
    ToolExecutionOutput,
    ToolSafetyLevel,
)
from sjtuclaw.runtime.skill_service import SkillService

_MAX_CHARS = 1_000_000


def _output_schema(properties: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": tuple(properties),
        "properties": properties,
    }


class ReadSkillResourceTool:
    @property
    def definition(self) -> ToolDefinition:
        schema: dict[str, JsonValue] = {
            "type": "object",
            "additionalProperties": False,
            "required": ("skill_id", "path", "max_chars"),
            "properties": {
                "skill_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "path": {"type": "string", "minLength": 1, "maxLength": 240},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": _MAX_CHARS},
                "cursor": {"type": "string", "minLength": 1, "maxLength": 64},
            },
        }
        return ToolDefinition(
            name="read_skill_resource",
            description="Read bounded UTF-8 text from a resource of an already selected local Skill.",
            input_schema=schema,
            output_schema=_output_schema(
                {
                    "skill_id": {
                        "type": "string",
                        "description": "Opaque Skill ID; not a URL.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Skill resource path; not a URL.",
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
            result_usage="Use the cursor metadata to continue reading content when truncated.",
            safety_level=ToolSafetyLevel.READ_ONLY,
        )

    def __init__(self, *, skills: SkillService) -> None:
        self._skills = skills

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        return (str(arguments["skill_id"]), str(arguments["path"]))

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        context = require_execution_context()
        skill_id = _string(arguments, "skill_id")
        path = _string(arguments, "path")
        max_chars = _integer(arguments, "max_chars")
        offset = _cursor(arguments.get("cursor"))
        try:
            content, next_offset = self._skills.read_resource(
                context.selected_skill_refs,
                skill_id,
                path,
                offset=offset,
                max_chars=max_chars,
            )
        except SkillError as exc:
            raise ToolExecutionError(exc.code) from exc
        next_cursor = None if next_offset is None else _encode_cursor(next_offset)
        result = {
            "skill_id": skill_id,
            "path": path,
            "content": content,
            "truncated": next_offset is not None,
            "next_cursor": next_cursor,
        }
        return ToolExecutionOutput(
            content=json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            structured_content={
                "skill_id": skill_id,
                "path": path,
                "truncated": next_offset is not None,
                "next_cursor": next_cursor,
            },
        )


def _string(arguments: Mapping[str, JsonValue], name: str) -> str:
    value = arguments[name]
    if not isinstance(value, str):
        raise ToolExecutionError("SKILL_RESOURCE_ARGUMENT_INVALID")
    return value


def _integer(arguments: Mapping[str, JsonValue], name: str) -> int:
    value = arguments[name]
    if type(value) is not int:
        raise ToolExecutionError("SKILL_RESOURCE_ARGUMENT_INVALID")
    return value


def _cursor(value: JsonValue | None) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        raise ToolExecutionError("SKILL_RESOURCE_CURSOR_INVALID")
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii")).decode("ascii")
        offset = int(decoded)
    except (UnicodeError, ValueError) as exc:
        raise ToolExecutionError("SKILL_RESOURCE_CURSOR_INVALID") from exc
    if offset < 0:
        raise ToolExecutionError("SKILL_RESOURCE_CURSOR_INVALID")
    return offset


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")
