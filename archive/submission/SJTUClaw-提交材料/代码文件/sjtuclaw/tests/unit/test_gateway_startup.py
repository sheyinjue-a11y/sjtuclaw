"""Gateway startup browser handoff checks."""

from __future__ import annotations

import asyncio
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

from pytest import MonkeyPatch

from sjtuclaw.entrypoints import gateway
from sjtuclaw.entrypoints.gateway_auth import BootstrapTokenRegistry
from sjtuclaw.runtime.active_session import ActiveSessionCoordinator


@dataclass
class _Server:
    started: bool = False
    should_exit: bool = False


def test_gateway_opens_fragment_bootstrap_url_only_after_startup(
    monkeypatch: MonkeyPatch,
) -> None:
    options = gateway.GatewayOptions(port=8766, bootstrap_token="a" * 43)
    server = _Server()
    opened: list[tuple[str, int]] = []

    def record_open(url: str, *, new: int) -> bool:
        opened.append((url, new))
        return True

    monkeypatch.setattr(webbrowser, "open", record_open)

    async def scenario() -> None:
        task = asyncio.create_task(
            gateway._open_bootstrap_page_when_ready(server, options)
        )
        await asyncio.sleep(0)
        assert opened == []
        server.started = True
        await task

    asyncio.run(scenario())

    assert opened == [(options.bootstrap_url, 2)]


def test_run_gateway_wires_runtime_memory_services_to_application(
    monkeypatch: MonkeyPatch,
) -> None:
    """Protect the runtime path that serves the Memory API from becoming unavailable."""

    memory_service = object()
    memory_policy = object()
    prompt_configuration = object()
    command_service = object()
    captured_gateway_arguments: dict[str, object] = {}
    captured_launcher_arguments: dict[str, object] = {}
    cancelled_tasks: set[str] = set()
    applications: list[Application] = []

    class Runtime:
        async def recover_pending(self) -> None:
            return None

    class Scheduler:
        async def recover(self) -> None:
            return None

        def start(self) -> None:
            return None

    class Application:
        def __init__(self) -> None:
            self.runtime = Runtime()
            self.scheduler = Scheduler()
            self.session_service = object()
            self.attachment_service = object()
            self.archive_service = object()
            self.workspace_service = object()
            self.downloads = object()
            self.skills = object()
            self.compaction = object()
            self.llm = object()
            self.memory_service = memory_service
            self.memory_policy = memory_policy
            self.prompts = prompt_configuration
            self.commands = command_service
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class ProviderSettings:
        def load(self) -> object:
            return object()

    class UvicornConfig:
        def __init__(self, app: object, **_kwargs: object) -> None:
            self.app = app

    class UvicornServer:
        started = True
        should_exit = False

        def __init__(self, _config: UvicornConfig) -> None:
            return None

        async def serve(self) -> None:
            await asyncio.sleep(0)

    def capture_gateway(**kwargs: object) -> object:
        captured_gateway_arguments.update(kwargs)
        return object()

    class Launcher:
        def __init__(self, **kwargs: object) -> None:
            captured_launcher_arguments.update(kwargs)

        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_tasks.add("pet")
                raise

    async def wait_for_server(_server: object, _options: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_tasks.add("browser")
            raise

    def build_application(*_args: object, **_kwargs: object) -> Application:
        application = Application()
        applications.append(application)
        return application

    monkeypatch.setattr(
        "sjtuclaw.entrypoints.gateway.Settings.load",
        lambda: SimpleNamespace(data_dir="data", model_profile=object()),
    )
    monkeypatch.setattr(gateway, "ProviderSettingsStore", lambda _path: ProviderSettings())
    monkeypatch.setattr(gateway, "build_cli_application", build_application)
    monkeypatch.setattr(gateway, "create_gateway", capture_gateway)
    monkeypatch.setattr(gateway, "_open_bootstrap_page_when_ready", wait_for_server)
    monkeypatch.setattr(gateway, "PetLauncher", Launcher)
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=UvicornConfig, Server=UvicornServer),
    )

    options = gateway.GatewayOptions(bootstrap_token="a" * 43)
    asyncio.run(gateway.run_gateway(options))

    assert captured_gateway_arguments["memories"] is memory_service
    assert captured_gateway_arguments["memory_policy"] is memory_policy
    assert captured_gateway_arguments["prompts"] is prompt_configuration
    assert captured_gateway_arguments["commands"] is command_service
    tokens = cast(BootstrapTokenRegistry, captured_gateway_arguments["bootstrap_tokens"])
    assert tokens.consume(options.bootstrap_token) == "browser"
    issue_pet_token = cast(Callable[[], str], captured_launcher_arguments["issue_pet_token"])
    pet_token = issue_pet_token()
    assert tokens.consume(pet_token) == "pet"
    active_sessions = cast(
        ActiveSessionCoordinator, captured_gateway_arguments["active_sessions"]
    )
    assert active_sessions._sessions is applications[0].session_service
    assert cancelled_tasks == {"browser", "pet"}
    assert applications[0].closed is True
