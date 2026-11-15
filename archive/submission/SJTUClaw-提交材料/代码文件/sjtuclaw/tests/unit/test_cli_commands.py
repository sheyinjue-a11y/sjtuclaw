import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from sjtuclaw.config import Settings
from sjtuclaw.entrypoints import cli as cli_module
from sjtuclaw.entrypoints.cli import CLIState, _handle_command, run_cli
from sjtuclaw.errors import StoreError
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.compaction import CompactionResult, CompactionStatus
from sjtuclaw.models.memory import Memory, MemoryEntry, MemoryPage
from sjtuclaw.models.runtime import InvocationSource, RuntimeEvent, RuntimeEventType
from sjtuclaw.models.skill import SkillManifest, SkillReference
from sjtuclaw.runtime import (
    MemoryCoordinator,
    MemoryService,
    SessionCoordinator,
    SessionService,
)
from tests.fakes import FakeClock, FakeIdGenerator


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self.memory = Memory()

    def load(self) -> Memory:
        return self.memory

    def replace(self, memory: Memory) -> Memory:
        self.memory = memory
        return memory

    def add(self, entry: MemoryEntry) -> Memory:
        self.memory = Memory(self.memory.entries + (entry,))
        return self.memory

    def delete(self, memory_id: str) -> Memory:
        self.memory = Memory(
            tuple(entry for entry in self.memory.entries if entry.memory_id != memory_id)
        )
        return self.memory


def _services() -> tuple[SessionService, MemoryService]:
    clock = FakeClock(datetime(2026, 7, 15, tzinfo=UTC))
    sessions = SessionService(
        sessions=InMemorySessionStore(),
        coordinator=SessionCoordinator(),
        clock=clock,
        ids=FakeIdGenerator(),
    )
    memory = MemoryService(
        InMemoryMemoryStore(),
        MemoryCoordinator(),
        clock=clock.now,
        new_id=lambda: "memory_001",
    )
    return sessions, memory


def test_session_commands_change_explicit_cli_selection() -> None:
    asyncio.run(_assert_session_commands_change_explicit_cli_selection())


async def _assert_session_commands_change_explicit_cli_selection() -> None:
    service, _ = _services()
    first = service.create()
    state = CLIState(first.session_id)
    output: list[str] = []

    assert await _handle_command("/session new", service, state, output.append)
    second_id = state.session_id
    assert second_id != first.session_id
    assert await _handle_command(
        f"/session rename {second_id} 课程问答", service, state, output.append
    )
    assert await _handle_command(
        f"/session switch {first.session_id}", service, state, output.append
    )
    assert state.session_id == first.session_id
    assert await _handle_command("/session list", service, state, output.append)
    assert await _handle_command(
        f"/session delete {first.session_id}", service, state, output.append
    )

    assert state.session_id == second_id
    assert service.load(second_id).title == "课程问答"
    assert any(line.startswith("*") for line in output)


def test_memory_and_unknown_commands_are_handled_without_llm() -> None:
    asyncio.run(_assert_memory_and_unknown_commands_are_handled_without_llm())


async def _assert_memory_and_unknown_commands_are_handled_without_llm() -> None:
    service, memory = _services()
    state = CLIState(service.create().session_id)
    output: list[str] = []

    assert await _handle_command(
        "/memory add 默认用中文",
        service,
        state,
        output.append,
        memory=memory,
    )
    memory_id = memory.list_page(query="", cursor=None, limit=100).entries[0].memory_id
    assert await _handle_command(
        "/memory list",
        service,
        state,
        output.append,
        memory=memory,
    )
    assert await _handle_command(
        f"/memory delete {memory_id}",
        service,
        state,
        output.append,
        memory=memory,
    )
    assert await _handle_command("/unknown", service, state, output.append)
    assert not await _handle_command("ordinary message", service, state, output.append)

    assert memory.list_page(query="", cursor=None, limit=100).entries == ()
    assert any("未知" in line for line in output)


def test_memory_list_outputs_all_pages() -> None:
    asyncio.run(_assert_memory_list_outputs_all_pages())


async def _assert_memory_list_outputs_all_pages() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    first = MemoryEntry("memory_001", "first", now)
    second = MemoryEntry("memory_002", "second", now)
    third = MemoryEntry("memory_003", "third", now)

    class PagedMemoryService:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        def list_page(
            self,
            *,
            query: str,
            cursor: str | None,
            limit: int,
        ) -> MemoryPage:
            assert query == ""
            assert limit == 10_000
            self.cursors.append(cursor)
            if cursor is None:
                return MemoryPage((first, second), "next")
            assert cursor == "next"
            return MemoryPage((third,), None)

    sessions, _ = _services()
    state = CLIState(sessions.create().session_id)
    output: list[str] = []
    memory = PagedMemoryService()

    assert await _handle_command(
        "/memory list",
        sessions,
        state,
        output.append,
        memory=memory,  # type: ignore[arg-type]
    )

    assert memory.cursors == [None, "next"]
    assert output == [
        "- memory_001 | first",
        "- memory_002 | second",
        "- memory_003 | third",
    ]


def test_cli_command_help_and_skill_show_use_shared_command_service() -> None:
    asyncio.run(_assert_cli_command_help_and_skill_show_use_shared_command_service())


async def _assert_cli_command_help_and_skill_show_use_shared_command_service() -> None:
    class Skills:
        def __init__(self) -> None:
            self.manifest = SkillManifest(
                skill_id="course-report",
                description="write a course report",
                version="1.0.0",
                snapshot_id="course-report-1111111111111111",
                snapshot_hash="a" * 64,
                available=True,
            )

        def list(self) -> tuple[SkillManifest, ...]:
            return (self.manifest,)

        def refresh(self) -> tuple[SkillManifest, ...]:
            return self.list()

        def resolve_current_names(self, _names: tuple[str, ...]) -> tuple[SkillReference, ...]:
            return (self.manifest.reference,)

    sessions, memory = _services()
    state = CLIState(sessions.create().session_id)
    output: list[str] = []
    skills = Skills()

    assert await _handle_command(
        "/command",
        sessions,
        state,
        output.append,
        skills=skills,  # type: ignore[arg-type]
        memory=memory,
    )
    assert any(line.startswith("/skill show <skill-name> |") for line in output)

    output.clear()
    assert await _handle_command(
        "/skill show course-report",
        sessions,
        state,
        output.append,
        skills=skills,  # type: ignore[arg-type]
        memory=memory,
    )
    assert output == ["Skill> $course-report v1.0.0", "write a course report"]


def test_compact_command_uses_shared_service_and_prints_preview() -> None:
    asyncio.run(_assert_compact_command_uses_shared_service_and_prints_preview())


async def _assert_compact_command_uses_shared_service_and_prints_preview() -> None:
    class FakeCompaction:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        async def compact_session(
            self,
            session_id: str,
            force: bool = True,
        ) -> CompactionResult:
            self.calls.append((session_id, force))
            return CompactionResult(
                status=CompactionStatus.COMPACTED,
                session_id=session_id,
                before_tokens=120,
                after_tokens=60,
                compacted_message_count=4,
                recent_message_count=8,
                summary_preview="保留了任务目标和当前状态",
                target_met=True,
                summary_prompt_tokens=18,
            )

    service, _ = _services()
    state = CLIState(service.create().session_id)
    compaction = FakeCompaction()
    output: list[str] = []

    assert await _handle_command(
        "/compact",
        service,
        state,
        output.append,
        compaction,
    )

    assert compaction.calls == [(state.session_id, True)]
    assert output == [
        "Compaction> 完成：120 -> 60 tokens，压缩 4 条消息，"
        "counter=deterministic，summary_prompt_tokens=18",
        "Summary> 保留了任务目标和当前状态",
    ]


def test_main_displays_startup_store_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail_to_start() -> None:
        raise StoreError("invalid JSON in session file")

    monkeypatch.setattr(cli_module, "run_cli", fail_to_start)

    cli_module.main()

    output = capsys.readouterr().out
    assert output == "Startup error: StoreError: invalid JSON in session file\n"
    assert "Traceback" not in output


def test_cli_surfaces_tool_events_without_reasoning_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_assert_cli_surfaces_tool_events(monkeypatch))


async def _assert_cli_surfaces_tool_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHandle:
        async def events(self) -> AsyncIterator[RuntimeEvent]:
            yield RuntimeEvent(
                RuntimeEventType.TOOL_CALL,
                "session_001",
                "request_001",
                1,
                InvocationSource.CLI,
                {"call_id": "call_001", "name": "read_file", "arguments": "{}"},
            )
            yield RuntimeEvent(
                RuntimeEventType.TOOL_RESULT,
                "session_001",
                "request_001",
                1,
                InvocationSource.CLI,
                {
                    "call_id": "call_001",
                    "tool": "read_file",
                    "content": "safe contents",
                    "status": "success",
                    "error_code": None,
                },
            )
            yield RuntimeEvent(
                RuntimeEventType.ASSISTANT_MESSAGE,
                "session_001",
                "request_001",
                1,
                InvocationSource.CLI,
                {"content": "final answer", "usage": {}},
            )
            yield RuntimeEvent(
                RuntimeEventType.RUN_COMPLETED,
                "session_001",
                "request_001",
                1,
                InvocationSource.CLI,
            )

    class FakeRuntime:
        async def submit(self, request: object) -> FakeHandle:
            return FakeHandle()

    class FakeApplication:
        initial_session_id = "session_001"
        runtime = FakeRuntime()
        session_service = object()
        memory_service = object()
        compaction = object()

        async def aclose(self) -> None:
            return None

    inputs = iter(("read a safe file", "/exit"))
    output: list[str] = []
    application = FakeApplication()
    monkeypatch.setattr(Settings, "load", lambda: object())
    monkeypatch.setattr(cli_module, "build_cli_application", lambda *args: application)
    monkeypatch.setattr(cli_module, "_load_prompt", lambda name: "prompt")

    await run_cli(input_fn=lambda prompt: next(inputs), output_fn=output.append)

    assert "Tool call> read_file {}" in output
    assert "Tool result> read_file success: safe contents" in output
    assert "Assistant> final answer" in output
    assert all("reasoning" not in line for line in output)
