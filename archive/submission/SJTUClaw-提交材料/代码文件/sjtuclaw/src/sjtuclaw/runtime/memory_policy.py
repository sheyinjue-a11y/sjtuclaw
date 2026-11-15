"""Runtime use cases for an editable long-term-memory policy."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from threading import RLock

from sjtuclaw.errors import ConfigurationError
from sjtuclaw.ports.memory import MemoryPolicy, MemoryPolicyStore

_DEFAULT_POLICY_REVISION = 1
_DEFAULT_RULES_FILE = "memory_rules.md"


class MemoryPolicyRevisionConflict(Exception):
    """The caller tried to change memory policy using an older revision."""


@dataclass(frozen=True, slots=True)
class MemoryPolicySnapshot:
    rules: str
    revision: int
    is_override: bool
    updated_at: datetime | None


class MemoryPolicyService:
    def __init__(
        self,
        store: MemoryPolicyStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()

    def get(self) -> MemoryPolicySnapshot:
        with self._lock:
            return self._current()

    def default_rules(self) -> str:
        """Return the packaged rules without exposing policy internals to callers."""
        return self._default_snapshot().rules

    def update(self, rules: str, *, revision: int) -> MemoryPolicySnapshot:
        with self._lock:
            current = self._current()
            self._require_current_revision(current, revision)
            saved = self._store.save(
                MemoryPolicy(rules, current.revision + 1, updated_at=self._clock())
            )
            return MemoryPolicySnapshot(
                saved.rules,
                saved.revision,
                is_override=True,
                updated_at=saved.updated_at,
            )

    def reset(self, *, revision: int) -> MemoryPolicySnapshot:
        with self._lock:
            current = self._current()
            self._require_current_revision(current, revision)
            self._store.reset()
            return self._default_snapshot()

    def _current(self) -> MemoryPolicySnapshot:
        override = self._store.load()
        if override is None:
            return self._default_snapshot()
        return MemoryPolicySnapshot(
            override.rules,
            override.revision,
            is_override=True,
            updated_at=override.updated_at,
        )

    @staticmethod
    def _require_current_revision(current: MemoryPolicySnapshot, revision: int) -> None:
        if type(revision) is not int or revision != current.revision:
            raise MemoryPolicyRevisionConflict("memory policy revision conflict")

    @staticmethod
    def _default_snapshot() -> MemoryPolicySnapshot:
        try:
            rules = (
                resources.files("sjtuclaw.prompts")
                .joinpath(_DEFAULT_RULES_FILE)
                .read_text(encoding="utf-8")
            )
            policy = MemoryPolicy(rules, _DEFAULT_POLICY_REVISION, datetime.now(UTC))
        except (FileNotFoundError, ModuleNotFoundError, OSError, ValueError) as exc:
            raise ConfigurationError("default memory policy is unavailable or invalid") from exc
        return MemoryPolicySnapshot(policy.rules, policy.revision, is_override=False, updated_at=None)
