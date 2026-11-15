import asyncio
import json
from typing import cast

import pytest

from sjtuclaw.infrastructure.tools.context import reset_execution_context, set_execution_context
from sjtuclaw.infrastructure.tools.registry import ToolRegistry
from sjtuclaw.infrastructure.tools.skills import ReadSkillResourceTool
from sjtuclaw.models.runtime import InvocationSource
from sjtuclaw.models.skill import SkillReference
from sjtuclaw.models.tool import (
    ToolCall,
    ToolExecutionContext,
    ToolExecutionOutput,
    mutable_json,
)
from sjtuclaw.ports.tools import ToolBatchValidationError
from sjtuclaw.runtime.skill_service import SkillService


def test_read_skill_resource_allows_one_million_characters() -> None:
    tool = ReadSkillResourceTool(skills=cast(SkillService, None))
    registry = ToolRegistry((tool,))

    intent = registry.validate_batch(
        (
            ToolCall(
                "call-1",
                "read_skill_resource",
                '{"skill_id":"course-report","path":"SKILL.md","max_chars":1000000}',
            ),
        )
    )[0]

    assert intent.arguments["max_chars"] == 1_000_000

    with pytest.raises(ToolBatchValidationError) as caught:
        registry.validate_batch(
            (
                ToolCall(
                    "call-2",
                    "read_skill_resource",
                    '{"skill_id":"course-report","path":"SKILL.md","max_chars":1000001}',
                ),
            )
        )

    assert caught.value.results[0].error_code == "SCHEMA_VALIDATION_FAILED"


class FakeSkillReader:
    def read_resource(
        self,
        references: tuple[SkillReference, ...],
        skill_id: str,
        path: str,
        *,
        offset: int,
        max_chars: int,
    ) -> tuple[str, int | None]:
        assert references[0].skill_id == "course-report"
        assert (skill_id, path, offset, max_chars) == (
            "course-report",
            "SKILL.md",
            0,
            4,
        )
        return "body", 4


def test_read_skill_resource_keeps_body_only_in_content_metadata() -> None:
    async def execute() -> ToolExecutionOutput:
        reference = SkillReference(
            "course-report",
            f"course-report-{'a' * 16}",
            "a" * 64,
            "1.0",
        )
        context = ToolExecutionContext(
            "session_001",
            "request_001",
            InvocationSource.CLI,
            selected_skill_snapshot_ids=(reference.snapshot_id,),
            selected_skill_refs=(reference,),
        )
        token = set_execution_context(context)
        try:
            return await ReadSkillResourceTool(
                skills=cast(SkillService, FakeSkillReader())
            ).execute(
                {
                    "skill_id": "course-report",
                    "path": "SKILL.md",
                    "max_chars": 4,
                }
            )
        finally:
            reset_execution_context(token)

    output = asyncio.run(execute())
    payload = json.loads(output.content)

    assert payload["content"] == "body"
    assert mutable_json(output.structured_content) == {
        "skill_id": "course-report",
        "path": "SKILL.md",
        "truncated": True,
        "next_cursor": "NA==",
    }
    assert "content" not in output.structured_content
    assert output.resources == ()
