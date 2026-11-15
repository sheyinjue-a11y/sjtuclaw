import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import pytest

from sjtuclaw.entrypoints.pet_launcher import PetLauncher


class Process(Protocol):
    async def wait(self) -> int: ...

    def terminate(self) -> None: ...


@dataclass
class FakeProcess:
    return_code: int
    terminated: bool = False

    async def wait(self) -> int:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True


class FakeProcessFactory:
    def __init__(self, return_codes: list[int]) -> None:
        self.return_codes = iter(return_codes)
        self.calls: list[SimpleNamespace] = []

    async def __call__(
        self, electron: str, main: str, *, env: dict[str, str]
    ) -> FakeProcess:
        self.calls.append(SimpleNamespace(electron=electron, main=main, env=env))
        return FakeProcess(next(self.return_codes))


def configured_launcher(
    root: Path,
    *,
    factory: FakeProcessFactory,
    issue: Callable[[], str],
) -> PetLauncher:
    electron = root / "node_modules/electron/dist/electron.exe"
    main = root / "dist-desktop/main.js"
    electron.parent.mkdir(parents=True)
    main.parent.mkdir(parents=True)
    electron.touch()
    main.touch()
    return PetLauncher(
        origin="http://127.0.0.1:8765",
        web_root=root,
        issue_pet_token=issue,
        create_process=factory,
    )


class BlockingProcess:
    def __init__(self) -> None:
        self.terminated = False
        self._stopped = asyncio.Event()

    async def wait(self) -> int:
        await self._stopped.wait()
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self._stopped.set()


class VanishedProcess:
    def __init__(self) -> None:
        self.wait_started = asyncio.Event()
        self.wait_completed = False
        self.wait_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        self.wait_started.set()
        if self.wait_calls == 1:
            await asyncio.Event().wait()
        self.wait_completed = True
        return 0

    def terminate(self) -> None:
        raise ProcessLookupError


def configured_launcher_with_callable(
    root: Path, create_process: Callable[..., Awaitable[Process]]
) -> PetLauncher:
    electron = root / "node_modules/electron/dist/electron.exe"
    main = root / "dist-desktop/main.js"
    electron.parent.mkdir(parents=True)
    main.parent.mkdir(parents=True)
    electron.touch()
    main.touch()
    return PetLauncher(
        origin="http://127.0.0.1:8765",
        web_root=root,
        issue_pet_token=lambda: "p" * 43,
        create_process=create_process,
    )


def test_missing_electron_returns_without_stopping_gateway(tmp_path: Path) -> None:
    warnings: list[str] = []
    launcher = PetLauncher(
        origin="http://127.0.0.1:8765",
        web_root=tmp_path,
        issue_pet_token=lambda: "p" * 43,
        warn=warnings.append,
    )
    asyncio.run(launcher.run())
    assert warnings == ["Electron desktop pet is unavailable; Gateway will continue"]


def test_spawn_failure_warns_without_stopping_gateway(tmp_path: Path) -> None:
    warnings: list[str] = []

    async def create_process(
        _electron: str, _main: str, *, env: dict[str, str]
    ) -> Process:
        raise OSError("broken Electron executable")

    electron = tmp_path / "node_modules/electron/dist/electron.exe"
    main = tmp_path / "dist-desktop/main.js"
    electron.parent.mkdir(parents=True)
    main.parent.mkdir(parents=True)
    electron.touch()
    main.touch()
    launcher = PetLauncher(
        origin="http://127.0.0.1:8765",
        web_root=tmp_path,
        issue_pet_token=lambda: "p" * 43,
        create_process=create_process,
        warn=warnings.append,
    )

    asyncio.run(launcher.run())

    assert warnings == ["Electron desktop pet is unavailable; Gateway will continue"]


def test_crash_restarts_once_with_a_fresh_token(tmp_path: Path) -> None:
    tokens = iter(("a" * 43, "b" * 43))
    factory = FakeProcessFactory(return_codes=[1, 1])
    launcher = configured_launcher(tmp_path, factory=factory, issue=lambda: next(tokens))
    asyncio.run(launcher.run())
    assert len(factory.calls) == 2
    assert (
        factory.calls[0].env["SJTUCLAW_PET_BOOTSTRAP_TOKEN"]
        != factory.calls[1].env["SJTUCLAW_PET_BOOTSTRAP_TOKEN"]
    )


def test_cancellation_terminates_child(tmp_path: Path) -> None:
    process = BlockingProcess()

    async def create_process(
        _electron: str, _main: str, *, env: dict[str, str]
    ) -> BlockingProcess:
        assert env["SJTUCLAW_PET_BOOTSTRAP_TOKEN"] == "p" * 43
        return process

    launcher = configured_launcher_with_callable(tmp_path, create_process)

    async def scenario() -> None:
        task = asyncio.create_task(launcher.run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert process.terminated is True


def test_cancellation_waits_when_child_exits_before_terminate(tmp_path: Path) -> None:
    process = VanishedProcess()

    async def create_process(
        _electron: str, _main: str, *, env: dict[str, str]
    ) -> VanishedProcess:
        return process

    launcher = configured_launcher_with_callable(tmp_path, create_process)

    async def scenario() -> None:
        task = asyncio.create_task(launcher.run())
        await process.wait_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert process.wait_calls == 2
    assert process.wait_completed is True
