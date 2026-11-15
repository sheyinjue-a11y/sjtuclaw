"""Pure default Skill candidate detection and request merging."""

from dataclasses import replace

from sjtuclaw.models.attachment import Attachment, AttachmentKind
from sjtuclaw.models.runtime import RuntimeRequest
from sjtuclaw.runtime.skill_service import SkillService

_PDF_MARKERS = ("pdf",)
_PRESENTATION_MARKERS = ("ppt", "powerpoint", "presentation", "\u5e7b\u706f\u7247", "\u6f14\u793a\u6587\u7a3f", "\u6c47\u62a5")


def candidate_default_skill_ids(
    text: str, attachments: tuple[Attachment, ...]
) -> tuple[str, ...]:
    """Return ordered built-in Skill IDs suggested by request facts alone."""

    text_casefolded = text.casefold()
    candidates: list[str] = []
    if any(attachment.kind is AttachmentKind.PDF for attachment in attachments) or any(
        marker in text_casefolded for marker in _PDF_MARKERS
    ):
        candidates.append("pdf")
    if any(marker in text_casefolded for marker in _PRESENTATION_MARKERS):
        candidates.append("presentations")
    return tuple(candidates)


def merge_default_skill_refs(
    request: RuntimeRequest,
    candidates: tuple[str, ...],
    skills: SkillService,
) -> RuntimeRequest:
    """Append available default snapshots without displacing selected Skills."""

    available = {manifest.skill_id: manifest for manifest in skills.list() if manifest.available}
    references = list(request.skill_refs)
    selected_ids = {reference.skill_id for reference in references}
    automatic_ids = list(request.automatic_skill_ids)
    for candidate in candidates:
        if len(references) == 3:
            break
        if candidate in selected_ids:
            continue
        manifest = available.get(candidate)
        if manifest is None:
            continue
        references.append(manifest.reference)
        selected_ids.add(candidate)
        automatic_ids.append(candidate)
    return replace(
        request,
        skill_refs=tuple(references),
        automatic_skill_ids=tuple(automatic_ids),
    )
