"""Strict, local-only Skill source discovery; it never executes Skill content."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sjtuclaw.models.skill import SkillManifest, SkillSnapshot

_MAX_SKILL_FILES = 32
_MAX_SKILL_BYTES = 96_000
_MAX_RESOURCE_BYTES = 24_000
_SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ParsedSkillDocument:
    skill_id: str
    description: str
    version: str
    instructions: str


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    snapshot: SkillSnapshot
    files: dict[str, bytes]


class SkillRegistry:
    """Read-only source scanner retained for local CLI and deterministic tests."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def scan(self) -> tuple[SkillManifest, ...]:
        if not self._root.is_dir():
            return ()
        manifests: list[SkillManifest] = []
        seen: set[str] = set()
        for directory in sorted(self._root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir() or directory.is_symlink():
                continue
            normalized = unicodedata.normalize("NFC", directory.name).casefold()
            if normalized in seen:
                raise ValueError("skill directory names collide after normalization")
            seen.add(normalized)
            try:
                manifests.append(self.candidate(directory).snapshot.manifest)
            except (OSError, UnicodeError, ValueError) as exc:
                skill_id = directory.name if _SKILL_ID.fullmatch(directory.name) else "invalid-skill"
                manifests.append(
                    SkillManifest(
                        skill_id=skill_id,
                        description=None,
                        version=None,
                        snapshot_id=None,
                        snapshot_hash=None,
                        available=False,
                        unavailable_reason=_safe_reason(exc),
                    )
                )
        return tuple(manifests)

    def candidate(
        self, directory: Path, *, published_at: datetime | None = None
    ) -> SkillCandidate:
        if directory.parent.resolve() != self._root or directory.is_symlink() or not directory.is_dir():
            raise ValueError("skill directory is invalid")
        files = _collect_files(directory)
        raw = files.get("SKILL.md")
        if raw is None:
            raise ValueError("skill is missing SKILL.md")
        parsed = parse_skill_document(raw.decode("utf-8"), expected_skill_id=directory.name)
        snapshot_hash = snapshot_hash_for_files(files)
        snapshot_id = f"{parsed.skill_id}-{snapshot_hash[:32]}"
        manifest = SkillManifest(
            skill_id=parsed.skill_id,
            description=parsed.description,
            version=parsed.version,
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            available=True,
        )
        if published_at is None:
            from datetime import UTC

            published_at = datetime.now(UTC)
        return SkillCandidate(
            snapshot=SkillSnapshot(manifest, parsed.instructions, published_at),
            files=files,
        )

    def read_resource(self, skill_id: str, relative_path: str, max_chars: int) -> str:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        if not _SKILL_ID.fullmatch(skill_id):
            raise ValueError("skill_id is invalid")
        path = _safe_relative_path(relative_path)
        target = self._root / skill_id / path
        if target.is_symlink() or not target.is_file():
            raise ValueError("skill resource must be a regular file")
        root = (self._root / skill_id).resolve()
        if root not in target.resolve().parents:
            raise ValueError("skill resource escapes selected skill")
        content = target.read_text(encoding="utf-8")
        return content[:max_chars]


def parse_skill_document(raw: str, *, expected_skill_id: str) -> ParsedSkillDocument:
    """Parse one bounded, deliberately small YAML-like frontmatter block."""

    lines = raw.splitlines()
    if len(lines) < 4 or lines[0] != "---":
        raise ValueError("SKILL.md requires frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if (
            not separator
            or key not in {"name", "description", "version"}
            or not value.strip()
            or key in fields
        ):
            raise ValueError("invalid SKILL.md frontmatter")
        fields[key] = value.strip()
    name = fields.get("name")
    description = fields.get("description")
    version = fields.get("version", "0")
    if name is None or description is None or name != expected_skill_id or not _SKILL_ID.fullmatch(name):
        raise ValueError("frontmatter name must match the skill directory")
    if len(description) > 280 or len(version) > 64:
        raise ValueError("skill frontmatter is too large")
    instructions = "\n".join(lines[end + 1 :]).strip()
    if not instructions:
        raise ValueError("SKILL.md instructions must not be empty")
    return ParsedSkillDocument(name, description, version, instructions)


def snapshot_hash_for_files(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(files):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[relative_path])
        digest.update(b"\0")
    return digest.hexdigest()


def collect_snapshot_files(root: Path) -> dict[str, bytes]:
    """Read a persisted snapshot with the same no-link/no-binary checks as source scan."""

    return _collect_files(root)


def _collect_files(directory: Path) -> dict[str, bytes]:
    root = directory.resolve(strict=True)
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        if "__pycache__" in path.parts:
            # Compiled caches are not Skill content and may be binary.
            continue
        if path.is_symlink():
            raise ValueError("skill symlinks are not supported")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("skill contains a non-regular file")
        resolved = path.resolve(strict=True)
        if root not in resolved.parents:
            raise ValueError("skill file escapes its root")
        relative = resolved.relative_to(root).as_posix()
        _safe_relative_path(relative)
        content = path.read_bytes()
        if len(content) > _MAX_RESOURCE_BYTES or b"\0" in content:
            raise ValueError("skill resource is too large or binary")
        total += len(content)
        if len(files) >= _MAX_SKILL_FILES or total > _MAX_SKILL_BYTES:
            raise ValueError("skill exceeds its bounded resource limit")
        files[relative] = content
    if "SKILL.md" not in files:
        raise ValueError("skill is missing SKILL.md")
    return files


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("skill resource path must be relative and contained")
    return path


def _safe_reason(error: Exception) -> str:
    return " ".join(str(error).split())[:240] or type(error).__name__


__all__ = [
    "SkillCandidate",
    "SkillManifest",
    "SkillRegistry",
    "collect_snapshot_files",
    "parse_skill_document",
    "snapshot_hash_for_files",
]
