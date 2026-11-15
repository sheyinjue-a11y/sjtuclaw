"""Strict, atomic persistence for a user memory-policy override."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from sjtuclaw.errors import StoreError
from sjtuclaw.infrastructure.persistence.json_store import _atomic_write, _read_json
from sjtuclaw.ports.memory import MemoryPolicy

_POLICY_FILE_NAME = "memory_policy.json"
_SCHEMA_VERSION = 1
_DOCUMENT_FIELDS = frozenset({"schemaVersion", "rules", "updatedAt", "revision"})


class JsonMemoryPolicyStore:
    """Persist a strict schema-v1 override in ``root/memory_policy.json``."""

    def __init__(self, root: Path) -> None:
        self._path = root / _POLICY_FILE_NAME

    def load(self) -> MemoryPolicy | None:
        if not self._path.exists():
            return None
        return _policy_from_json(_read_json(self._path))

    def save(self, policy: MemoryPolicy) -> MemoryPolicy:
        if policy.revision < 2:
            raise StoreError("memory policy override revision must be at least 2")
        if self._path.exists():
            self.load()
        _atomic_write(self._path, _policy_to_json(policy))
        return policy

    def reset(self) -> None:
        if not self._path.exists():
            return
        try:
            self._path.unlink()
        except OSError as exc:
            raise StoreError(f"failed to delete {_POLICY_FILE_NAME}: {exc}") from exc


def _policy_to_json(policy: MemoryPolicy) -> dict[str, object]:
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "rules": policy.rules,
        "updatedAt": policy.updated_at.isoformat(),
        "revision": policy.revision,
    }


def _policy_from_json(value: object) -> MemoryPolicy:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise StoreError("memory policy document must be an object")
    document = cast(dict[str, object], value)
    if frozenset(document) != _DOCUMENT_FIELDS:
        raise StoreError(
            "memory policy document has invalid fields: "
            f"expected {sorted(_DOCUMENT_FIELDS)}, got {sorted(document)}"
        )
    schema_version = document["schemaVersion"]
    if type(schema_version) is not int:
        raise StoreError("memory policy document.schemaVersion must be an integer")
    if schema_version != _SCHEMA_VERSION:
        raise StoreError(f"unsupported schemaVersion in memory policy document: {schema_version}")
    rules = document["rules"]
    if type(rules) is not str:
        raise StoreError("memory policy document.rules must be a string")
    revision = document["revision"]
    if type(revision) is not int or revision < 2:
        raise StoreError("memory policy document.revision must be an integer of at least 2")
    updated_at = _utc_timestamp(document["updatedAt"])
    try:
        return MemoryPolicy(rules=rules, revision=revision, updated_at=updated_at)
    except ValueError as exc:
        raise StoreError("memory policy document has invalid rules") from exc


def _utc_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise StoreError("memory policy document.updatedAt must be an ISO timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StoreError("memory policy document.updatedAt must be an ISO timestamp") from exc
    if (
        timestamp.tzinfo is None
        or timestamp.utcoffset() is None
        or timestamp.utcoffset() != timedelta(0)
    ):
        raise StoreError("memory policy document.updatedAt must be timezone-aware UTC")
    return timestamp
