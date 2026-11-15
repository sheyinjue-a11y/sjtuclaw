import asyncio
import json
from pathlib import Path
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from sjtuclaw.infrastructure.tools.skill_selection import SelectSkillTool
from sjtuclaw.models.attachment import Attachment, AttachmentKind, AttachmentStatus
from sjtuclaw.models.runtime import InvocationSource, RuntimeRequest, UserInput
from sjtuclaw.models.skill import SkillManifest, SkillReference
from sjtuclaw.models.tool import ToolExecutionOutput, mutable_json
from sjtuclaw.runtime.skill_routing import candidate_default_skill_ids, merge_default_skill_refs
from sjtuclaw.runtime.skill_service import SkillService

NOW = datetime(2026, 7, 29, tzinfo=UTC)


class FakeSkills:
    def __init__(self, manifests: tuple[SkillManifest, ...]) -> None:
        self._manifests = manifests

    def list(self) -> tuple[SkillManifest, ...]:
        return self._manifests


def ready_pdf_attachment() -> Attachment:
    return Attachment(
        "attachment_001",
        "session_001",
        "source.pdf",
        AttachmentKind.PDF,
        AttachmentStatus.READY,
        NOW,
        NOW,
        size_bytes=1,
        sha256="a" * 64,
        normalized_char_count=1,
    )


def reference(skill_id: str) -> SkillReference:
    return SkillReference(skill_id, f"{skill_id}-{'a' * 16}", "b" * 64, "1.0")


def manifest(skill_id: str) -> SkillManifest:
    skill_reference = reference(skill_id)
    return SkillManifest(
        skill_id,
        "test skill",
        skill_reference.version,
        skill_reference.snapshot_id,
        skill_reference.snapshot_hash,
        True,
    )


def request_with_refs(*skill_ids: str) -> RuntimeRequest:
    return RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.GATEWAY,
        UserInput("create report"),
        skill_refs=tuple(reference(skill_id) for skill_id in skill_ids),
    )


def test_pdf_attachment_routes_pdf_and_presentation_text_routes_presentations() -> None:
    assert candidate_default_skill_ids("\u8bf7\u751f\u6210 PPT \u6c47\u62a5", ()) == ("presentations",)
    assert candidate_default_skill_ids("\u603b\u7ed3\u9644\u4ef6", (ready_pdf_attachment(),)) == ("pdf",)


def test_explicit_refs_win_and_auto_refs_fill_only_remaining_slots() -> None:
    request = request_with_refs("course-report", "material-summary", "presentation-outline")
    skills = cast(SkillService, FakeSkills((manifest("pdf"), manifest("presentations"))))

    routed = merge_default_skill_refs(request, ("pdf", "presentations"), skills)

    assert routed.skill_refs == request.skill_refs
    assert routed.automatic_skill_ids == ()


def test_available_candidates_append_once_and_mark_only_added_references() -> None:
    request = replace(request_with_refs("course-report"), automatic_skill_ids=())
    skills = cast(SkillService, FakeSkills((manifest("pdf"), manifest("presentations"))))

    routed = merge_default_skill_refs(request, ("pdf", "pdf", "presentations"), skills)

    assert tuple(reference.skill_id for reference in routed.skill_refs) == (
        "course-report",
        "pdf",
        "presentations",
    )
    assert routed.automatic_skill_ids == ("pdf", "presentations")


class FakeSkillSelector:
    def resolve_current_names(self, skill_ids: tuple[str, ...]) -> tuple[SkillReference, ...]:
        return tuple(reference(skill_id) for skill_id in skill_ids)

    def install_path(self, skill_id: str) -> Path:
        return Path("C:/skills") / skill_id


def test_select_skill_returns_compact_content_and_structured_reference() -> None:
    async def execute() -> ToolExecutionOutput:
        return await SelectSkillTool(
            skills=cast(SkillService, FakeSkillSelector())
        ).execute(
            {
                "skill_id": "course-report",
                "reason": "needed for the report",
            }
        )

    output = asyncio.run(execute())
    expected = {
        "skill_id": "course-report",
        "snapshot_id": f"course-report-{'a' * 16}",
        "version": "1.0",
        "install_path": str(Path("C:/skills") / "course-report"),
    }

    assert json.loads(output.content) == expected
    assert mutable_json(output.structured_content) == expected
    assert output.resources == ()
