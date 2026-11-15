from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest

from sjtuclaw.errors import ConfigurationError, StoreError
from sjtuclaw.infrastructure.persistence.memory_policy import JsonMemoryPolicyStore
from sjtuclaw.ports.memory import MemoryPolicy
from sjtuclaw.runtime.memory_policy import MemoryPolicyRevisionConflict, MemoryPolicyService


def _service(tmp_path: Path) -> MemoryPolicyService:
    return MemoryPolicyService(JsonMemoryPolicyStore(tmp_path))


def test_default_policy_is_packaged_and_has_stable_snapshot_revision(tmp_path: Path) -> None:
    snapshot = _service(tmp_path).get()

    assert snapshot.is_override is False
    assert snapshot.revision == 1
    assert snapshot.updated_at is None
    assert "A 类" in snapshot.rules
    assert "C 类" in snapshot.rules
    assert "短期" in snapshot.rules
    assert "未确认" in snapshot.rules
    assert "相对时间" in snapshot.rules


def test_first_override_starts_at_revision_two_and_later_updates_increment(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = service.update("保留用户明确的长期偏好。", revision=1)
    second = service.update("保留稳定且可验证的跨会话事实。", revision=2)

    assert first.is_override is True
    assert first.revision == 2
    assert second.is_override is True
    assert second.revision == 3
    assert second.updated_at is not None
    assert JsonMemoryPolicyStore(tmp_path).load() == MemoryPolicy(
        rules="保留稳定且可验证的跨会话事实。",
        revision=3,
        updated_at=second.updated_at,
    )
    document = json.loads((tmp_path / "memory_policy.json").read_text(encoding="utf-8"))
    assert set(document) == {"schemaVersion", "rules", "updatedAt", "revision"}
    assert datetime.fromisoformat(document["updatedAt"]).utcoffset() == UTC.utcoffset(None)


def test_update_rejects_blank_long_and_control_character_rules_without_overwriting(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    saved = service.update("原有规则。", revision=1)

    for invalid_rules in ("   ", "x" * 12_001, "规则\x00", "规则\x7f", "规则\x85"):
        with pytest.raises(ValueError, match="memory policy rules are invalid"):
            service.update(invalid_rules, revision=saved.revision)

    assert service.get() == saved


def test_update_rejects_stale_revision_without_changing_rules(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.update("原有规则。", revision=1)

    with pytest.raises(MemoryPolicyRevisionConflict):
        service.update("冲突规则。", revision=1)

    assert service.get() == saved


def test_store_rejects_non_v1_or_non_exact_override_document(tmp_path: Path) -> None:
    path = tmp_path / "memory_policy.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "rules": "规则",
                "updatedAt": "2026-07-28T12:00:00+00:00",
                "revision": 2,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StoreError, match="unsupported schemaVersion"):
        JsonMemoryPolicyStore(tmp_path).load()
    with pytest.raises(StoreError, match="unsupported schemaVersion"):
        JsonMemoryPolicyStore(tmp_path).save(
            MemoryPolicy("替换规则。", 2, datetime(2026, 7, 28, tzinfo=UTC))
        )
    assert json.loads(path.read_text(encoding="utf-8"))["schemaVersion"] == 2

    path.write_text(
        json.dumps(
            {"schemaVersion": 1, "rules": "规则", "revision": 2, "unexpected": True}
        ),
        encoding="utf-8",
    )

    with pytest.raises(StoreError, match="invalid fields"):
        JsonMemoryPolicyStore(tmp_path).load()

    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "rules": "规则",
                "updatedAt": "2026-07-28T12:00:00+08:00",
                "revision": 2,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StoreError, match="timezone-aware UTC"):
        JsonMemoryPolicyStore(tmp_path).load()


def test_invalid_update_does_not_overwrite_existing_override(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.update("已保存规则。", revision=1)
    assert saved.updated_at is not None

    with pytest.raises(ValueError, match="memory policy rules are invalid"):
        service.update("\x1f", revision=saved.revision)

    assert JsonMemoryPolicyStore(tmp_path).load() == MemoryPolicy(
        "已保存规则。", 2, saved.updated_at
    )


def test_reset_requires_current_revision_and_deletes_only_override_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.update("可重置规则。", revision=1)
    sibling = tmp_path / "memory_policy.json.bak"
    sibling.write_text("keep", encoding="utf-8")

    with pytest.raises(MemoryPolicyRevisionConflict):
        service.reset(revision=1)

    reset = service.reset(revision=saved.revision)

    assert reset.is_override is False
    assert reset.revision == 1
    assert not (tmp_path / "memory_policy.json").exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_missing_or_blank_packaged_default_is_configuration_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingRules:
        def joinpath(self, _name: str) -> MissingRules:
            return self

        def read_text(self, *, encoding: str) -> str:
            raise FileNotFoundError

    def missing_files(_package: object) -> MissingRules:
        return MissingRules()

    monkeypatch.setattr(resources, "files", missing_files)

    with pytest.raises(ConfigurationError, match="default memory policy"):
        _service(tmp_path).get()

    class BlankRules:
        def joinpath(self, _name: str) -> BlankRules:
            return self

        def read_text(self, *, encoding: str) -> str:
            return " \n\t"

    def blank_files(_package: object) -> BlankRules:
        return BlankRules()

    monkeypatch.setattr(resources, "files", blank_files)

    with pytest.raises(ConfigurationError, match="default memory policy"):
        _service(tmp_path).get()


def test_write_failure_keeps_previous_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    saved = service.update("已保存规则。", revision=1)

    import sjtuclaw.infrastructure.persistence.memory_policy as memory_policy

    def fail_write(_path: Path, _document: dict[str, object]) -> None:
        raise StoreError("write failed")

    monkeypatch.setattr(memory_policy, "_atomic_write", fail_write)

    with pytest.raises(StoreError, match="write failed"):
        service.update("新规则。", revision=saved.revision)

    assert service.get() == saved
