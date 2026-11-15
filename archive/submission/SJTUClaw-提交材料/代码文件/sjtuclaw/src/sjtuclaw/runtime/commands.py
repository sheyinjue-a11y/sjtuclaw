"""Shared local command catalog and execution service for CLI and Gateway."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from sjtuclaw.errors import SkillError, StoreError
from sjtuclaw.models.compaction import CompactionResult, CompactionStatus
from sjtuclaw.models.memory import CreateMemory
from sjtuclaw.models.skill import SkillManifest, SkillReference
from sjtuclaw.runtime.memory_service import MemoryService
from sjtuclaw.runtime.session_service import SessionService


@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    """One user-visible command template used by CLI help and Web completion."""

    template: str
    description: str
    skill_argument: bool = False


@dataclass(frozen=True, slots=True)
class CommandSubmission:
    """A normal Run requested by an explicit Skill command."""

    text: str
    skill_refs: tuple[SkillReference, ...]


@dataclass(frozen=True, slots=True)
class CommandEffects:
    """Entry-point effects that are intentionally outside command execution."""

    refresh_sessions: bool = False
    refresh_memories: bool = False
    refresh_skills: bool = False
    select_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Bounded command result with optional UI refresh or normal Run request."""

    raw: str
    lines: tuple[str, ...]
    is_error: bool = False
    effects: CommandEffects = field(default_factory=CommandEffects)
    submission: CommandSubmission | None = None


class CommandCatalog:
    """The one canonical public command list; `/exit` intentionally stays CLI-only."""

    def descriptors(self) -> tuple[CommandDescriptor, ...]:
        return (
            CommandDescriptor("/command", "List all local commands."),
            CommandDescriptor("/session new", "Create and select a new Session."),
            CommandDescriptor("/session list", "List active Sessions."),
            CommandDescriptor("/session switch <id>", "Select an existing Session."),
            CommandDescriptor("/session rename <id> <title>", "Rename a Session."),
            CommandDescriptor("/session delete <id>", "Delete a Session."),
            CommandDescriptor("/memory list", "List long-term memories."),
            CommandDescriptor("/memory add <content>", "Create a manual long-term memory."),
            CommandDescriptor("/memory delete <id>", "Delete a manual long-term memory."),
            CommandDescriptor("/compact", "Compact the selected Session."),
            CommandDescriptor("/skill list", "List local Skills."),
            CommandDescriptor("/skill show <skill-name>", "Show one local Skill.", True),
            CommandDescriptor(
                "/skill <skill-name> <task>",
                "Run a task with one explicit local Skill.",
                True,
            ),
            CommandDescriptor("/skill usage", "Show selected Session Skill usage."),
            CommandDescriptor("/skill refresh", "Refresh local Skill snapshots."),
        )


class CompactionCommands(Protocol):
    async def compact_session(
        self,
        session_id: str,
        force: bool = True,
    ) -> CompactionResult: ...


class SkillCommands(Protocol):
    def list(self) -> tuple[SkillManifest, ...]: ...

    def refresh(self) -> tuple[SkillManifest, ...]: ...

    def resolve_current_names(
        self,
        names: Iterable[str],
    ) -> tuple[SkillReference, ...]: ...


class CommandService:
    """Parse and execute all shared local commands without entry-point side effects."""

    def __init__(
        self,
        *,
        sessions: SessionService,
        memories: MemoryService | None = None,
        compaction: CompactionCommands | None = None,
        skills: SkillCommands | None = None,
        catalog: CommandCatalog | None = None,
    ) -> None:
        self._sessions = sessions
        self._memories = memories
        self._compaction = compaction
        self._skills = skills
        self._catalog = catalog or CommandCatalog()

    def descriptors(self) -> tuple[CommandDescriptor, ...]:
        return self._catalog.descriptors()

    async def execute(
        self,
        raw: str,
        *,
        current_session_id: str,
    ) -> CommandOutcome | None:
        """Return ``None`` for chat text and a safe Outcome for every slash command."""

        command = raw.strip()
        if not command.startswith("/"):
            return None
        if command == "/command":
            return CommandOutcome(
                command,
                tuple(f"{item.template} | {item.description}" for item in self.descriptors()),
            )
        try:
            session_outcome = await self._execute_session(command, current_session_id)
            if session_outcome is not None:
                return session_outcome
            memory_outcome = self._execute_memory(command)
            if memory_outcome is not None:
                return memory_outcome
            compaction_outcome = await self._execute_compaction(command, current_session_id)
            if compaction_outcome is not None:
                return compaction_outcome
            skill_outcome = self._execute_skill(command, current_session_id)
            if skill_outcome is not None:
                return skill_outcome
        except SkillError as error:
            return _error(command, f"Skill> {error.code}")
        except StoreError:
            return _error(command, "Error> requested local resource is unavailable")
        except ValueError:
            return _error(command, "Error> command arguments are invalid")
        return _error(command, f"Error> 未知或参数不完整的命令：{command}")

    async def _execute_session(
        self,
        command: str,
        current_session_id: str,
    ) -> CommandOutcome | None:
        if command == "/session new":
            session = self._sessions.create()
            return CommandOutcome(
                command,
                (f"Session> created and selected {session.session_id}",),
                effects=CommandEffects(
                    refresh_sessions=True,
                    select_session_id=session.session_id,
                ),
            )
        if command == "/session list":
            sessions = self._sessions.list()
            if not sessions:
                return CommandOutcome(command, ("Session> (empty)",))
            return CommandOutcome(
                command,
                tuple(
                    f"{'*' if session.session_id == current_session_id else ' '} "
                    f"{session.session_id} | {session.title} | {session.updated_at.isoformat()}"
                    for session in sessions
                ),
            )

        parts = command.split(maxsplit=3)
        if len(parts) == 3 and parts[:2] == ["/session", "switch"]:
            session = self._sessions.load(parts[2])
            return CommandOutcome(
                command,
                (f"Session> selected {session.session_id}",),
                effects=CommandEffects(select_session_id=session.session_id),
            )
        if len(parts) == 4 and parts[:2] == ["/session", "rename"]:
            title = parts[3].strip()
            if not title:
                return _error(command, "Error> Session title must not be empty")
            session = await self._sessions.rename(parts[2], title)
            return CommandOutcome(
                command,
                (f"Session> renamed {session.session_id} to {session.title}",),
                effects=CommandEffects(refresh_sessions=True),
            )
        if len(parts) == 3 and parts[:2] == ["/session", "delete"]:
            deleted_id = parts[2]
            await self._sessions.delete(deleted_id)
            selection = current_session_id
            if deleted_id == current_session_id:
                remaining = self._sessions.list()
                selected = remaining[0] if remaining else self._sessions.create()
                selection = selected.session_id
            return CommandOutcome(
                command,
                (f"Session> deleted {deleted_id}",),
                effects=CommandEffects(refresh_sessions=True, select_session_id=selection),
            )
        return None

    def _execute_memory(self, command: str) -> CommandOutcome | None:
        if not command.startswith("/memory"):
            return None
        if self._memories is None:
            return _error(command, "Memory> service is unavailable")
        if command == "/memory list":
            cursor: str | None = None
            entries: list[str] = []
            while True:
                page = self._memories.list_page(query="", cursor=cursor, limit=10_000)
                entries.extend(f"- {entry.memory_id} | {entry.content}" for entry in page.entries)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
            return CommandOutcome(command, tuple(entries) or ("Memory> (empty)",))
        parts = command.split(maxsplit=2)
        if len(parts) == 3 and parts[:2] == ["/memory", "add"]:
            entry = self._memories.create_manual(CreateMemory(parts[2]))
            return CommandOutcome(
                command,
                (f"Memory> added {entry.memory_id}",),
                effects=CommandEffects(refresh_memories=True),
            )
        if len(parts) == 3 and parts[:2] == ["/memory", "delete"]:
            self._memories.delete_manual(parts[2], revision=None)
            return CommandOutcome(
                command,
                (f"Memory> deleted {parts[2]}",),
                effects=CommandEffects(refresh_memories=True),
            )
        return _error(command, f"Error> 未知或参数不完整的命令：{command}")

    async def _execute_compaction(
        self,
        command: str,
        current_session_id: str,
    ) -> CommandOutcome | None:
        if command != "/compact":
            return None
        if self._compaction is None:
            return _error(command, "Compaction> service is unavailable")
        result = await self._compaction.compact_session(current_session_id, force=True)
        return _compaction_outcome(command, result)

    def _execute_skill(
        self,
        command: str,
        current_session_id: str,
    ) -> CommandOutcome | None:
        if not command.startswith("/skill"):
            return None
        if self._skills is None:
            return _error(command, "Skill> service is unavailable")
        if command == "/skill list":
            lines = tuple(_skill_list_line(manifest) for manifest in self._skills.list())
            return CommandOutcome(command, lines or ("Skill> (empty)",))
        if command == "/skill usage":
            session = self._sessions.load(current_session_id)
            lines = tuple(
                f"- ${usage.reference.skill_id} v{usage.reference.version} | "
                f"{usage.source.value} #{usage.selection_order} | {usage.reason}"
                for usage in session.skill_usages
            )
            return CommandOutcome(command, lines or ("Skill> (no usage)",))
        if command == "/skill refresh":
            manifests = self._skills.refresh()
            return CommandOutcome(
                command,
                (f"Skill> refreshed {len(manifests)} local Skill manifests",),
                effects=CommandEffects(refresh_skills=True),
            )

        parts = command.split(maxsplit=2)
        if len(parts) == 3 and parts[:2] == ["/skill", "show"]:
            manifest = next(
                (item for item in self._skills.list() if item.skill_id == parts[2]),
                None,
            )
            if manifest is None:
                return _error(command, "Skill> not found")
            if not manifest.available:
                return _error(command, f"Skill> unavailable: {manifest.unavailable_reason}")
            assert manifest.version is not None
            assert manifest.description is not None
            return CommandOutcome(
                command,
                (f"Skill> ${manifest.skill_id} v{manifest.version}", manifest.description),
            )
        if len(parts) == 3 and parts[:1] == ["/skill"]:
            references = self._skills.resolve_current_names((parts[1],))
            return CommandOutcome(
                command,
                (f"Skill> selected ${parts[1]} for this request",),
                submission=CommandSubmission(parts[2], references),
            )
        return _error(command, f"Error> 未知或参数不完整的命令：{command}")


def _skill_list_line(manifest: SkillManifest) -> str:
    """Render public metadata without depending on editable Skill source content."""

    if manifest.available:
        assert manifest.version is not None
        assert manifest.description is not None
        return f"- ${manifest.skill_id} v{manifest.version} | {manifest.description}"
    return f"- ${manifest.skill_id} unavailable | {manifest.unavailable_reason}"


def _compaction_outcome(command: str, result: CompactionResult) -> CommandOutcome:
    if result.status is CompactionStatus.COMPACTED:
        return CommandOutcome(
            command,
            (
                "Compaction> 完成："
                f"{result.before_tokens} -> {result.after_tokens} tokens，"
                f"压缩 {result.compacted_message_count} 条消息，"
                f"counter={result.token_counter_mode}，"
                f"summary_prompt_tokens={result.summary_prompt_tokens}",
                f"Summary> {result.summary_preview}",
            ),
        )
    if result.status is CompactionStatus.FAILED:
        return _error(command, f"Compaction error> {result.error_type}: {result.error_message}")
    return CommandOutcome(command, ("Compaction> no eligible messages",))


def _error(raw: str, line: str) -> CommandOutcome:
    return CommandOutcome(raw, (line,), is_error=True)
