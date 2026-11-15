import asyncio
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from sjtuclaw.errors import SkillError
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.compaction import CompactionResult, CompactionStatus
from sjtuclaw.models.memory import Memory, MemoryEntry
from sjtuclaw.models.skill import SkillManifest, SkillReference, SkillUsage, SkillUsageSource
from sjtuclaw.runtime.commands import CommandService
from sjtuclaw.runtime.memory_coordinator import MemoryCoordinator
from sjtuclaw.runtime.memory_service import MemoryService
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_service import SessionService
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
        self.memory = Memory((*self.memory.entries, entry))
        return self.memory

    def delete(self, memory_id: str) -> Memory:
        self.memory = Memory(
            tuple(entry for entry in self.memory.entries if entry.memory_id != memory_id)
        )
        return self.memory


class FakeSkills:
    def __init__(self) -> None:
        self.manifests = (
            SkillManifest(
                skill_id="course-report",
                description="write a course report",
                version="1.0.0",
                snapshot_id="course-report-1111111111111111",
                snapshot_hash="a" * 64,
                available=True,
            ),
            SkillManifest(
                skill_id="broken-skill",
                description=None,
                version=None,
                snapshot_id=None,
                snapshot_hash=None,
                available=False,
                unavailable_reason="invalid frontmatter",
            ),
        )
        self.refresh_count = 0

    def list(self) -> tuple[SkillManifest, ...]:
        return self.manifests

    def refresh(self) -> tuple[SkillManifest, ...]:
        self.refresh_count += 1
        return self.manifests

    def resolve_current_names(self, names: Iterable[str]) -> tuple[SkillReference, ...]:
        available = {manifest.skill_id: manifest for manifest in self.manifests if manifest.available}
        try:
            return tuple(available[name].reference for name in names)
        except KeyError as error:
            raise SkillError("SKILL_UNAVAILABLE") from error


class FakeCompaction:
    def __init__(self, result: CompactionResult) -> None:
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    async def compact_session(self, session_id: str, force: bool = True) -> CompactionResult:
        self.calls.append((session_id, force))
        return self.result


def _services() -> tuple[SessionService, InMemorySessionStore, MemoryService, FakeSkills]:
    clock = FakeClock(datetime(2026, 7, 29, tzinfo=UTC))
    sessions_store = InMemorySessionStore()
    sessions = SessionService(
        sessions=sessions_store,
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
    return sessions, sessions_store, memory, FakeSkills()


def _compaction_result(status: CompactionStatus = CompactionStatus.COMPACTED) -> CompactionResult:
    return CompactionResult(
        status=status,
        session_id="session_001",
        before_tokens=120,
        after_tokens=60,
        compacted_message_count=4,
        recent_message_count=3,
        summary_preview="kept task and decisions",
        target_met=True,
        summary_prompt_tokens=12,
    )


def test_command_catalog_and_unknown_command() -> None:
    asyncio.run(_assert_command_catalog_and_unknown_command())


async def _assert_command_catalog_and_unknown_command() -> None:
    sessions, _store, memory, skills = _services()
    current = sessions.create()
    service = CommandService(sessions=sessions, memories=memory, skills=skills)

    listed = await service.execute("/command", current_session_id=current.session_id)
    unknown = await service.execute("/not-a-command", current_session_id=current.session_id)

    assert listed is not None
    assert listed.is_error is False
    assert any(line.startswith("/skill show <skill-name> |") for line in listed.lines)
    assert "/exit" not in "\n".join(listed.lines)
    assert unknown is not None
    assert unknown.is_error is True
    assert "未知或参数不完整的命令" in unknown.lines[0]
    assert await service.execute("ordinary message", current_session_id=current.session_id) is None


def test_session_commands_return_refresh_and_selection_effects() -> None:
    asyncio.run(_assert_session_commands_return_refresh_and_selection_effects())


async def _assert_session_commands_return_refresh_and_selection_effects() -> None:
    sessions, _store, memory, skills = _services()
    first = sessions.create()
    service = CommandService(sessions=sessions, memories=memory, skills=skills)

    created = await service.execute("/session new", current_session_id=first.session_id)
    assert created is not None
    assert created.effects.refresh_sessions is True
    assert created.effects.select_session_id == "session_002"

    renamed = await service.execute(
        "/session rename session_002 coursework", current_session_id="session_002"
    )
    switched = await service.execute("/session switch session_001", current_session_id="session_002")
    listed = await service.execute("/session list", current_session_id="session_001")
    deleted = await service.execute("/session delete session_001", current_session_id="session_001")

    assert renamed is not None and renamed.is_error is False
    assert switched is not None and switched.effects.select_session_id == "session_001"
    assert listed is not None and any(line.startswith("*") for line in listed.lines)
    assert deleted is not None and deleted.effects.select_session_id == "session_002"
    assert sessions.load("session_002").title == "coursework"


def test_memory_and_compaction_commands_are_bounded_outcomes() -> None:
    asyncio.run(_assert_memory_and_compaction_commands_are_bounded_outcomes())


async def _assert_memory_and_compaction_commands_are_bounded_outcomes() -> None:
    sessions, _store, memory, skills = _services()
    current = sessions.create()
    compaction = FakeCompaction(_compaction_result())
    service = CommandService(
        sessions=sessions,
        memories=memory,
        compaction=compaction,
        skills=skills,
    )

    created = await service.execute("/memory add exam is Friday", current_session_id=current.session_id)
    listed = await service.execute("/memory list", current_session_id=current.session_id)
    compacted = await service.execute("/compact", current_session_id=current.session_id)
    deleted = await service.execute("/memory delete memory_001", current_session_id=current.session_id)

    assert created is not None and created.effects.refresh_memories is True
    assert listed is not None and listed.lines == ("- memory_001 | exam is Friday",)
    assert compacted is not None and "120 -> 60 tokens" in compacted.lines[0]
    assert compaction.calls == [(current.session_id, True)]
    assert deleted is not None and deleted.effects.refresh_memories is True


def test_skill_commands_use_dynamic_skill_ids_and_submit_explicit_reference() -> None:
    asyncio.run(_assert_skill_commands_use_dynamic_skill_ids_and_submit_explicit_reference())


async def _assert_skill_commands_use_dynamic_skill_ids_and_submit_explicit_reference() -> None:
    sessions, store, memory, skills = _services()
    current = sessions.create()
    manifest = skills.list()[0]
    store.save(
        replace(
            current,
            skill_usages=(
                SkillUsage(
                    reference=manifest.reference,
                    source=SkillUsageSource.EXPLICIT,
                    selection_order=1,
                    reason="explicit command",
                    selected_at=datetime(2026, 7, 29, tzinfo=UTC),
                    request_id="request_001",
                    user_task="summarize lecture",
                ),
            ),
        )
    )
    service = CommandService(sessions=sessions, memories=memory, skills=skills)

    listed = await service.execute("/skill list", current_session_id=current.session_id)
    shown = await service.execute("/skill show course-report", current_session_id=current.session_id)
    usage = await service.execute("/skill usage", current_session_id=current.session_id)
    submitted = await service.execute(
        "/skill course-report summarize lecture", current_session_id=current.session_id
    )
    refreshed = await service.execute("/skill refresh", current_session_id=current.session_id)

    assert listed is not None and "$course-report v1.0.0" in listed.lines[0]
    assert shown is not None and shown.lines == (
        "Skill> $course-report v1.0.0",
        "write a course report",
    )
    assert usage is not None and "$course-report v1.0.0" in usage.lines[0]
    assert submitted is not None and submitted.submission is not None
    assert submitted.submission.text == "summarize lecture"
    assert submitted.submission.skill_refs == (manifest.reference,)
    assert refreshed is not None and refreshed.effects.refresh_skills is True
    assert skills.refresh_count == 1


def test_command_errors_are_outcomes_without_service_paths() -> None:
    asyncio.run(_assert_command_errors_are_outcomes_without_service_paths())


async def _assert_command_errors_are_outcomes_without_service_paths() -> None:
    sessions, _store, _memory, skills = _services()
    current = sessions.create()
    service = CommandService(sessions=sessions, memories=None, compaction=None, skills=skills)

    missing_memory = await service.execute("/memory list", current_session_id=current.session_id)
    missing_compaction = await service.execute("/compact", current_session_id=current.session_id)
    missing_skill = await service.execute("/skill show does-not-exist", current_session_id=current.session_id)
    missing_argument = await service.execute("/session rename session_001", current_session_id=current.session_id)

    assert missing_memory is not None and missing_memory.is_error is True
    assert missing_compaction is not None and missing_compaction.is_error is True
    assert missing_skill is not None and missing_skill.is_error is True
    assert missing_argument is not None and missing_argument.is_error is True
