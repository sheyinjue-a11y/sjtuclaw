"""Persistence boundary for immutable local Skill snapshots."""

from typing import Protocol

from sjtuclaw.models.skill import SkillManifest, SkillReference, SkillSnapshot


class SkillSnapshotStore(Protocol):
    def load_manifest(self) -> tuple[SkillManifest, ...]: ...

    def save_manifest(self, manifests: tuple[SkillManifest, ...]) -> None: ...

    def publish_snapshot(
        self,
        snapshot: SkillSnapshot,
        files: dict[str, bytes],
    ) -> None: ...

    def load_snapshot(self, reference: SkillReference) -> SkillSnapshot: ...

    def read_resource(
        self,
        reference: SkillReference,
        relative_path: str,
        *,
        offset: int,
        max_chars: int,
    ) -> tuple[str, int | None]: ...
