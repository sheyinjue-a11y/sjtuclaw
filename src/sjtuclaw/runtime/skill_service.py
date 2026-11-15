"""One process-local service for local Skill refresh, snapshots, and references."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from sjtuclaw.errors import SkillError, StoreError
from sjtuclaw.models.skill import SkillManifest, SkillReference, SkillSnapshot
from sjtuclaw.ports.runtime import Clock
from sjtuclaw.ports.skills import SkillSnapshotStore
from sjtuclaw.skills.registry import SkillRegistry

_MENTION = re.compile(r"(?<![\\A-Za-z0-9-])\$([a-z][a-z0-9-]{0,63})(?![A-Za-z0-9-])")
_MAX_SKILL_CONTEXT_CHARS = 24_000


class SkillService:
    """Safe facade: source is only read during refresh; Runs read fixed snapshots."""

    def __init__(self, *, source_root: Path, snapshots: SkillSnapshotStore, clock: Clock) -> None:
        self._source_root = source_root
        self._snapshots = snapshots
        self._clock = clock
        self._current = snapshots.load_manifest()

    def list(self) -> tuple[SkillManifest, ...]:
        return self._current

    def refresh(self) -> tuple[SkillManifest, ...]:
        if not self._source_root.is_dir() or self._source_root.is_symlink():
            raise SkillError("SKILL_ROOT_UNAVAILABLE")
        scanner = SkillRegistry(self._source_root)
        manifests: list[SkillManifest] = []
        candidates = []
        try:
            directories = tuple(
                sorted(
                    (
                        path
                        for path in self._source_root.iterdir()
                        if path.is_dir() and not path.is_symlink()
                    ),
                    key=lambda item: item.name,
                )
            )
        except OSError as exc:
            raise SkillError("SKILL_ROOT_UNAVAILABLE") from exc
        normalized = [directory.name.casefold() for directory in directories]
        if len(normalized) != len(set(normalized)):
            raise SkillError("SKILL_NAME_COLLISION")
        for directory in directories:
            try:
                candidate = scanner.candidate(directory, published_at=self._clock.now())
            except (OSError, UnicodeError, ValueError) as exc:
                skill_id = directory.name
                if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", skill_id):
                    # A malformed directory is unavailable but does not hide valid Skills.
                    skill_id = "invalid-skill"
                manifests.append(
                    SkillManifest(
                        skill_id=skill_id,
                        description=None,
                        version=None,
                        snapshot_id=None,
                        snapshot_hash=None,
                        available=False,
                        unavailable_reason=_reason(exc),
                    )
                )
                continue
            candidates.append(candidate)
            manifests.append(candidate.snapshot.manifest)
        identifiers = [item.skill_id for item in manifests]
        if len(identifiers) != len(set(identifiers)):
            raise SkillError("SKILL_NAME_COLLISION")
        try:
            for candidate in candidates:
                self._snapshots.publish_snapshot(candidate.snapshot, candidate.files)
            ordered = tuple(sorted(manifests, key=lambda item: item.skill_id))
            self._snapshots.save_manifest(ordered)
        except StoreError as exc:
            # Current remains untouched until the complete candidate manifest is durable.
            raise SkillError("SKILL_REFRESH_FAILED") from exc
        self._current = ordered
        return self._current

    def resolve_current_names(self, names: Iterable[str]) -> tuple[SkillReference, ...]:
        requested = tuple(
            name[1:] if name.startswith("$") else name for name in names
        )
        if len(requested) > 3 or len(requested) != len(set(requested)):
            raise SkillError("SKILL_SELECTION_LIMIT")
        available = {item.skill_id: item for item in self._current if item.available}
        references: list[SkillReference] = []
        for name in requested:
            manifest = available.get(name)
            if manifest is None:
                raise SkillError("SKILL_UNAVAILABLE")
            references.append(manifest.reference)
        return tuple(references)

    def validate_snapshot_refs(
        self, references: Iterable[SkillReference]
    ) -> tuple[SkillSnapshot, ...]:
        values = tuple(references)
        if len(values) > 3 or len({reference.skill_id for reference in values}) != len(values):
            raise SkillError("SKILL_SELECTION_LIMIT")
        snapshots: list[SkillSnapshot] = []
        for reference in values:
            try:
                snapshot = self._snapshots.load_snapshot(reference)
            except StoreError as exc:
                raise SkillError("SKILL_SNAPSHOT_UNAVAILABLE") from exc
            if snapshot.manifest.reference != reference:
                raise SkillError("SKILL_SNAPSHOT_UNAVAILABLE")
            snapshots.append(snapshot)
        return tuple(snapshots)

    def context_index(self) -> str:
        available = [item for item in self._current if item.available]
        if not available:
            return "Local Skill index is empty."
        return "Local Skill index (select only when useful):\n" + "\n".join(
            f"- ${item.skill_id} v{item.version}: {item.description} "
            f"(install: {self.install_path(item.skill_id)})"
            for item in available
        )

    def install_path(self, skill_id: str) -> Path:
        """Return the absolute on-disk source directory of an available Skill."""

        directory = (self._source_root / skill_id).resolve()
        if directory.is_symlink() or not directory.is_dir():
            raise SkillError("SKILL_UNAVAILABLE")
        return directory

    def instructions(self, references: Iterable[SkillReference]) -> tuple[SkillSnapshot, ...]:
        snapshots = self.validate_snapshot_refs(references)
        if sum(len(item.instructions) for item in snapshots) > _MAX_SKILL_CONTEXT_CHARS:
            raise SkillError("SKILL_CONTEXT_BUDGET_EXCEEDED")
        return snapshots

    def read_resource(
        self,
        references: Iterable[SkillReference],
        skill_id: str,
        relative_path: str,
        *,
        offset: int,
        max_chars: int,
    ) -> tuple[str, int | None]:
        reference = next(
            (item for item in references if item.skill_id == skill_id),
            None,
        )
        if reference is None:
            raise SkillError("SKILL_NOT_SELECTED")
        try:
            return self._snapshots.read_resource(
                reference,
                relative_path,
                offset=offset,
                max_chars=max_chars,
            )
        except StoreError as exc:
            raise SkillError("SKILL_RESOURCE_UNAVAILABLE") from exc

    @staticmethod
    def mention_names(text: str) -> tuple[str, ...]:
        names: list[str] = []
        for match in _MENTION.finditer(text):
            name = match.group(1)
            if name not in names:
                names.append(name)
        return tuple(names)

    @staticmethod
    def unescape_mentions(text: str) -> str:
        """Keep an escaped mention visible to the model without selecting it."""

        return text.replace("\\$", "$")


def _reason(error: Exception) -> str:
    return " ".join(str(error).split())[:240] or type(error).__name__
