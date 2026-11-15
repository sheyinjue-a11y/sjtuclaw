"""Windows Electron launcher for the local SJTUClaw desktop pet."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LOGGER = logging.getLogger(__name__)


class _Process(Protocol):
    async def wait(self) -> int: ...

    def terminate(self) -> None: ...


CreateProcess = Callable[..., Awaitable[_Process]]


@dataclass(frozen=True, slots=True)
class _DesktopPaths:
    electron: Path
    main: Path


async def _create_process(
    electron: str, main: str, *, env: dict[str, str]
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(electron, main, env=env)


class PetLauncher:
    """Launch the bundled Electron pet without exposing bootstrap tokens."""

    def __init__(
        self,
        *,
        origin: str,
        web_root: Path,
        issue_pet_token: Callable[[], str],
        create_process: CreateProcess = _create_process,
        warn: Callable[[str], None] = LOGGER.warning,
    ) -> None:
        self._origin = origin
        self._web_root = web_root
        self._issue_pet_token = issue_pet_token
        self._create_process = create_process
        self._warn = warn
        self._process: _Process | None = None

    async def run(self) -> None:
        paths = self._discover()
        if paths is None:
            self._warn("Electron desktop pet is unavailable; Gateway will continue")
            return

        for attempt in range(2):
            env = os.environ.copy()
            env["SJTUCLAW_GATEWAY_ORIGIN"] = self._origin
            env["SJTUCLAW_PET_BOOTSTRAP_TOKEN"] = self._issue_pet_token()
            try:
                process = await self._create_process(
                    str(paths.electron), str(paths.main), env=env
                )
            except OSError:
                self._warn("Electron desktop pet is unavailable; Gateway will continue")
                return
            self._process = process
            try:
                return_code = await process.wait()
            except asyncio.CancelledError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                await process.wait()
                raise
            finally:
                self._process = None

            if return_code == 0:
                return
            if attempt == 0:
                self._warn("Electron desktop pet exited; restarting once")

        self._warn("Electron desktop pet exited twice; restart disabled")

    def _discover(self) -> _DesktopPaths | None:
        if sys.platform != "win32":
            return None

        electron = self._web_root / "node_modules/electron/dist/electron.exe"
        main = self._web_root / "dist-desktop/main.js"
        if not electron.is_file() or not main.is_file():
            return None
        return _DesktopPaths(electron=electron, main=main)
