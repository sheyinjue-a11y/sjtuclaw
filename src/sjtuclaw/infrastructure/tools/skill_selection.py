"""Provider-independent structured local Skill selection Tool."""

from __future__ import annotations

import json
from collections.abc import Mapping

from sjtuclaw.errors import SkillError, ToolExecutionError
from sjtuclaw.models.tool import (
    JsonValue,
    ToolDefinition,
    ToolExecutionOutput,
    ToolSafetyLevel,
)
from sjtuclaw.runtime.skill_service import SkillService


def _output_schema(properties: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": tuple(properties),
        "properties": properties,
    }


class SelectSkillTool:
    """A read-only structured selection request; AgentLoop records its audit usage."""

    def __init__(self, *, skills: SkillService) -> None:
        self._skills = skills

    @property
    def definition(self) -> ToolDefinition:
        schema: dict[str, JsonValue] = {
            "type": "object",
            "additionalProperties": False,
            "required": ("skill_id", "reason"),
            "properties": {
                "skill_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "Exact local Skill ID; a leading $ mention prefix is accepted.",
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        }
        return ToolDefinition(
            name="select_skill",
            description="Select one useful local Skill by its exact ID and give a short reason.",
            input_schema=schema,
            output_schema=_output_schema(
                {
                    "skill_id": {
                        "type": "string",
                        "description": "Opaque Skill ID; not a URL.",
                    },
                    "snapshot_id": {
                        "type": "string",
                        "description": "Opaque immutable snapshot ID; not a URL.",
                    },
                    "version": {"type": "string"},
                    "install_path": {
                        "type": "string",
                        "description": (
                            "Absolute on-disk source directory of the selected "
                            "Skill; use it to run Skill scripts via the shell."
                        ),
                    },
                }
            ),
            result_usage="Use the selected immutable Skill reference for this run.",
            safety_level=ToolSafetyLevel.READ_ONLY,
        )

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
        return ()

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput:
        skill_id = arguments["skill_id"]
        if not isinstance(skill_id, str):
            raise ToolExecutionError("SKILL_SELECTION_ARGUMENT_INVALID")
        try:
            reference = self._skills.resolve_current_names((skill_id,))[0]
        except SkillError as exc:
            raise ToolExecutionError(exc.code) from exc
        result = {
            "skill_id": reference.skill_id,
            "snapshot_id": reference.snapshot_id,
            "version": reference.version,
            "install_path": str(self._skills.install_path(reference.skill_id)),
        }
        return ToolExecutionOutput(
            content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            structured_content=result,
        )
