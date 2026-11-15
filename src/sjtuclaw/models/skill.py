"""Immutable local Skill snapshot and usage contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

_SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SNAPSHOT_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}-[0-9a-f]{16,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SkillUsageSource(StrEnum):
    """How a trusted Skill snapshot entered a Run."""

    EXPLICIT = "explicit"
    AUTO = "auto"
    SCHEDULED_EXPLICIT = "scheduled_explicit"


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Public, bounded metadata for a local Skill's current snapshot."""

    skill_id: str
    description: str | None
    version: str | None
    snapshot_id: str | None
    snapshot_hash: str | None
    available: bool
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not _SKILL_ID.fullmatch(self.skill_id):
            raise ValueError("skill manifest skill_id is invalid")
        if self.available:
            if (
                self.description is None
                or self.version is None
                or self.snapshot_id is None
                or self.snapshot_hash is None
            ):
                raise ValueError("available skill manifest requires complete snapshot metadata")
            SkillReference(
                self.skill_id,
                self.snapshot_id,
                self.snapshot_hash,
                self.version,
            )
        elif self.unavailable_reason is None or not self.unavailable_reason.strip():
            raise ValueError("unavailable skill manifest requires a reason")

    @property
    def reference(self) -> SkillReference:
        if not self.available or self.snapshot_id is None or self.snapshot_hash is None or self.version is None:
            raise ValueError("skill is unavailable")
        return SkillReference(self.skill_id, self.snapshot_id, self.snapshot_hash, self.version)


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    """Current immutable snapshot facts, without trusting the editable source tree."""

    manifest: SkillManifest
    instructions: str
    published_at: datetime

    def __post_init__(self) -> None:
        if not self.manifest.available:
            raise ValueError("skill snapshot requires an available manifest")
        if not self.instructions.strip():
            raise ValueError("skill snapshot instructions must not be empty")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() != UTC.utcoffset(self.published_at):
            raise ValueError("skill snapshot published_at must be UTC")


@dataclass(frozen=True, slots=True)
class SkillReference:
    """A verified, immutable content-addressed Skill snapshot reference."""

    skill_id: str
    snapshot_id: str
    snapshot_hash: str
    version: str

    def __post_init__(self) -> None:
        if not _SKILL_ID.fullmatch(self.skill_id):
            raise ValueError("skill_id is invalid")
        if not _SNAPSHOT_ID.fullmatch(self.snapshot_id):
            raise ValueError("skill snapshot_id is invalid")
        if not _SHA256.fullmatch(self.snapshot_hash):
            raise ValueError("skill snapshot_hash is invalid")
        if not self.version.strip() or len(self.version) > 64:
            raise ValueError("skill version is invalid")


@dataclass(frozen=True, slots=True)
class SkillUsage:
    """Small audit record; Skill content is deliberately never copied here."""

    reference: SkillReference
    source: SkillUsageSource
    selection_order: int
    reason: str
    selected_at: datetime
    request_id: str
    user_task: str
    output_message_id: str | None = None
    resource_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.selection_order < 1:
            raise ValueError("skill usage selection_order must be positive")
        if not self.reason.strip() or len(self.reason) > 500:
            raise ValueError("skill usage reason is invalid")
        if self.selected_at.tzinfo is None or self.selected_at.utcoffset() != timedelta(0):
            raise ValueError("skill usage selected_at must be UTC")
        if not self.request_id.strip() or not self.user_task.strip():
            raise ValueError("skill usage request and task must not be empty")
        if self.output_message_id is not None and not self.output_message_id.strip():
            raise ValueError("skill usage output_message_id must not be empty")
        if any(not path or path.startswith("/") or ".." in path.split("/") for path in self.resource_paths):
            raise ValueError("skill usage resource paths must be relative and contained")
