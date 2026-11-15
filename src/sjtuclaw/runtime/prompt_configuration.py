"""User-owned System Prompt and Soul Markdown configuration."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock


@dataclass(frozen=True, slots=True)
class PromptSnapshot:
    """One consistent pair of prompt layers and their local file locations."""

    system_prompt: str
    soul: str
    system_prompt_path: Path
    soul_path: Path


class PromptConfigurationService:
    """Create user prompt files once and keep the active Soul hot-swappable."""

    def __init__(
        self,
        *,
        root: Path,
        default_system_prompt: str,
        default_soul: str,
    ) -> None:
        self._root = root
        self._system_prompt_path = root / "system_prompt.md"
        self._soul_path = root / "soul.md"
        self._lock = RLock()
        self._root.mkdir(parents=True, exist_ok=True)
        self._create_if_missing(self._system_prompt_path, default_system_prompt, required=True)
        self._create_if_missing(self._soul_path, default_soul, required=False)
        self._system_prompt = self._read(self._system_prompt_path, required=True)
        self._soul = self._read(self._soul_path, required=False)

    @property
    def system_prompt_path(self) -> Path:
        return self._system_prompt_path

    @property
    def soul_path(self) -> Path:
        return self._soul_path

    def snapshot(self) -> PromptSnapshot:
        with self._lock:
            return PromptSnapshot(
                system_prompt=self._system_prompt,
                soul=self._soul,
                system_prompt_path=self._system_prompt_path,
                soul_path=self._soul_path,
            )

    def update_soul(self, content: str) -> PromptSnapshot:
        normalized = _normalize(content, required=False)
        with self._lock:
            _atomic_write(self._soul_path, normalized)
            self._soul = normalized
            return self.snapshot()

    def _create_if_missing(self, path: Path, content: str, *, required: bool) -> None:
        if path.exists():
            return
        _atomic_write(path, _normalize(content, required=required))

    @staticmethod
    def _read(path: Path, *, required: bool) -> str:
        try:
            return _normalize(path.read_text(encoding="utf-8"), required=required)
        except OSError as exc:
            raise ValueError(f"prompt configuration is unavailable: {path}") from exc


def _normalize(content: str, *, required: bool) -> str:
    if "\x00" in content:
        raise ValueError("prompt configuration contains a NUL character")
    normalized = content.strip()
    if required and not normalized:
        raise ValueError("system prompt must not be empty")
    return normalized


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary:
            temporary.write(f"{content}\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
