from pathlib import Path

import pytest

from sjtuclaw.skills import SkillRegistry


def test_scan_keeps_bad_skill_unavailable_without_hiding_valid_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path, "valid", "valid", "usable")
    _write_skill(tmp_path, "bad", "different", "broken")

    manifests = SkillRegistry(tmp_path).scan()

    assert [(item.skill_id, item.available) for item in manifests] == [
        ("bad", False),
        ("valid", True),
    ]
    assert manifests[1].snapshot_hash is not None


def test_read_resource_rejects_escaping_path_and_bounds_text(tmp_path: Path) -> None:
    _write_skill(tmp_path, "valid", "valid", "usable")
    resource = tmp_path / "valid" / "guide.txt"
    resource.write_text("abcdef", encoding="utf-8")
    registry = SkillRegistry(tmp_path)

    assert registry.read_resource("valid", "guide.txt", 3) == "abc"
    with pytest.raises(ValueError, match="relative"):
        registry.read_resource("valid", "../outside.txt", 3)


def _write_skill(root: Path, directory: str, name: str, description: str) -> None:
    target = root / directory
    target.mkdir()
    (target / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody\n",
        encoding="utf-8",
    )
