"""Atomic content-addressed storage for local Skill snapshots."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from sjtuclaw.errors import StoreError
from sjtuclaw.models.skill import SkillManifest, SkillReference, SkillSnapshot

_MANIFEST_VERSION = 1
_MANIFEST_FIELDS = frozenset({"schemaVersion", "skills"})
_SKILL_FIELDS = frozenset(
    {
        "skillId",
        "description",
        "version",
        "snapshotId",
        "snapshotHash",
        "available",
        "unavailableReason",
    }
)


class JsonSkillSnapshotStore:
    """One small manifest plus immutable snapshot directories below data_dir."""

    def __init__(self, root: Path) -> None:
        self._root = root / "skill-snapshots"
        self._manifest_path = self._root / "manifest.json"

    def load_manifest(self) -> tuple[SkillManifest, ...]:
        if not self._manifest_path.exists():
            return ()
        try:
            raw = _read_json(self._manifest_path)
            if not isinstance(raw, dict) or frozenset(raw) != _MANIFEST_FIELDS:
                raise ValueError("invalid skill manifest document")
            if raw["schemaVersion"] != _MANIFEST_VERSION or not isinstance(raw["skills"], list):
                raise ValueError("unsupported skill manifest document")
            manifests = tuple(_manifest_from_json(item) for item in raw["skills"])
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            raise StoreError("skill manifest is invalid") from exc
        skill_ids = [manifest.skill_id for manifest in manifests]
        if len(skill_ids) != len(set(skill_ids)):
            raise StoreError("skill manifest contains duplicate IDs")
        return manifests

    def save_manifest(self, manifests: tuple[SkillManifest, ...]) -> None:
        identifiers = [manifest.skill_id for manifest in manifests]
        if len(identifiers) != len(set(identifiers)):
            raise StoreError("skill manifest contains duplicate IDs")
        _atomic_json(
            self._manifest_path,
            {
                "schemaVersion": _MANIFEST_VERSION,
                "skills": [_manifest_to_json(manifest) for manifest in manifests],
            },
        )

    def publish_snapshot(self, snapshot: SkillSnapshot, files: dict[str, bytes]) -> None:
        reference = snapshot.manifest.reference
        target = self._snapshot_path(reference)
        if target.exists():
            existing = self.load_snapshot(reference)
            if existing.instructions != snapshot.instructions:
                raise StoreError("content-addressed skill snapshot conflicts with existing data")
            return
        if "SKILL.md" not in files:
            raise StoreError("skill snapshot is missing SKILL.md")
        self._root.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".skill-", dir=self._root.parent))
        try:
            for relative_path, content in files.items():
                destination = temporary / _safe_relative_path(relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(temporary, target)
            except FileExistsError:
                existing = self.load_snapshot(reference)
                if existing.instructions != snapshot.instructions:
                    raise StoreError(
                        "content-addressed skill snapshot conflicts with existing data"
                    ) from None
                shutil.rmtree(temporary, ignore_errors=True)
        except OSError as exc:
            raise StoreError("failed to publish skill snapshot") from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def load_snapshot(self, reference: SkillReference) -> SkillSnapshot:
        target = self._snapshot_path(reference)
        try:
            skill_file = target / "SKILL.md"
            raw = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StoreError("skill snapshot is unavailable") from exc
        if target.is_symlink() or skill_file.is_symlink():
            raise StoreError("skill snapshot is unavailable")
        from sjtuclaw.skills.registry import (
            collect_snapshot_files,
            parse_skill_document,
            snapshot_hash_for_files,
        )

        try:
            files = collect_snapshot_files(target)
            parsed = parse_skill_document(raw, expected_skill_id=reference.skill_id)
            snapshot_hash = snapshot_hash_for_files(files)
            if snapshot_hash != reference.snapshot_hash or target.name != reference.snapshot_id:
                raise ValueError("snapshot metadata does not match its content")
            manifest = SkillManifest(
                skill_id=parsed.skill_id,
                description=parsed.description,
                version=parsed.version,
                snapshot_id=reference.snapshot_id,
                snapshot_hash=snapshot_hash,
                available=True,
            )
            published_at = datetime.fromtimestamp(skill_file.stat().st_mtime, tz=UTC)
            return SkillSnapshot(manifest, parsed.instructions, published_at)
        except (OSError, ValueError) as exc:
            raise StoreError("skill snapshot is invalid") from exc

    def read_resource(
        self,
        reference: SkillReference,
        relative_path: str,
        *,
        offset: int,
        max_chars: int,
    ) -> tuple[str, int | None]:
        if offset < 0 or max_chars < 1:
            raise StoreError("skill resource bounds are invalid")
        snapshot_root = self._snapshot_path(reference)
        target = snapshot_root / _safe_relative_path(relative_path)
        try:
            if snapshot_root.is_symlink() or target.is_symlink() or not target.is_file():
                raise StoreError("skill resource is unavailable")
            resolved_root = snapshot_root.resolve(strict=True)
            resolved_target = target.resolve(strict=True)
            if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
                raise StoreError("skill resource escapes its snapshot")
            raw = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StoreError("skill resource is unavailable") from exc
        if offset > len(raw):
            raise StoreError("skill resource cursor is invalid")
        end = min(len(raw), offset + max_chars)
        return raw[offset:end], None if end == len(raw) else end

    def _snapshot_path(self, reference: SkillReference) -> Path:
        return self._root / reference.snapshot_id


def _safe_relative_path(value: str) -> Path:
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in value
    ):
        raise StoreError("skill resource path must be a contained POSIX relative path")
    return Path(*relative.parts)


def _manifest_to_json(manifest: SkillManifest) -> dict[str, object]:
    return {
        "skillId": manifest.skill_id,
        "description": manifest.description,
        "version": manifest.version,
        "snapshotId": manifest.snapshot_id,
        "snapshotHash": manifest.snapshot_hash,
        "available": manifest.available,
        "unavailableReason": manifest.unavailable_reason,
    }


def _manifest_from_json(value: object) -> SkillManifest:
    if not isinstance(value, dict) or frozenset(value) != _SKILL_FIELDS:
        raise ValueError("invalid skill manifest entry")
    try:
        return SkillManifest(
            skill_id=_string(value["skillId"]),
            description=_optional_string(value["description"]),
            version=_optional_string(value["version"]),
            snapshot_id=_optional_string(value["snapshotId"]),
            snapshot_hash=_optional_string(value["snapshotHash"]),
            available=_bool(value["available"]),
            unavailable_reason=_optional_string(value["unavailableReason"]),
        )
    except ValueError as exc:
        raise ValueError("invalid skill manifest entry") from exc


def _atomic_json(path: Path, document: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise StoreError("failed to save skill manifest") from exc


def _read_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return cast(object, json.load(handle))


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("skill manifest value is invalid")
    return value


def _optional_string(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("skill manifest value is invalid")
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("skill manifest value is invalid")
    return value
