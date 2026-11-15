from pathlib import Path

import pytest

from sjtuclaw.errors import SkillError
from sjtuclaw.infrastructure.persistence.skills import JsonSkillSnapshotStore
from sjtuclaw.infrastructure.runtime import SystemClock
from sjtuclaw.runtime.skill_service import SkillService


def test_refresh_keeps_existing_run_snapshot_after_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, instructions="first instructions", resource="first resource")
    service = SkillService(
        source_root=source,
        snapshots=JsonSkillSnapshotStore(tmp_path / "data"),
        clock=SystemClock(),
    )

    first = service.refresh()[0].reference
    _write_skill(source, instructions="second instructions", resource="second resource")
    second = service.refresh()[0].reference

    assert first != second
    assert service.instructions((first,))[0].instructions == "first instructions"
    assert service.instructions((second,))[0].instructions == "second instructions"


def test_resource_reads_are_selected_snapshot_only_and_bounded(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, instructions="instructions", resource="abcdef")
    service = SkillService(
        source_root=source,
        snapshots=JsonSkillSnapshotStore(tmp_path / "data"),
        clock=SystemClock(),
    )
    reference = service.refresh()[0].reference

    assert service.read_resource((reference,), "material-summary", "guide.txt", offset=1, max_chars=3) == (
        "bcd",
        4,
    )
    with pytest.raises(SkillError, match="SKILL_NOT_SELECTED"):
        service.read_resource((), "material-summary", "guide.txt", offset=0, max_chars=1)
    with pytest.raises(SkillError, match="SKILL_RESOURCE_UNAVAILABLE"):
        service.read_resource((reference,), "material-summary", "../guide.txt", offset=0, max_chars=1)


def test_resolve_current_names_accepts_mention_prefix(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, instructions="instructions", resource="resource")
    service = SkillService(
        source_root=source,
        snapshots=JsonSkillSnapshotStore(tmp_path / "data"),
        clock=SystemClock(),
    )
    service.refresh()

    assert service.resolve_current_names(("$material-summary",))[0].skill_id == "material-summary"
    assert service.resolve_current_names(("material-summary",))[0].skill_id == "material-summary"


def _write_skill(root: Path, *, instructions: str, resource: str) -> None:
    directory = root / "material-summary"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: material-summary\n"
        "description: summarize supplied materials\n"
        "version: 1.0.0\n"
        "---\n"
        f"{instructions}\n",
        encoding="utf-8",
    )
    (directory / "guide.txt").write_text(resource, encoding="utf-8")
