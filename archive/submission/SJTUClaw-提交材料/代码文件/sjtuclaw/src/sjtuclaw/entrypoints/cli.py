"""Interactive CLI entrypoint backed by the shared local command service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Protocol

from sjtuclaw.bootstrap import build_cli_application
from sjtuclaw.config import Settings
from sjtuclaw.errors import ConfigurationError, SJTUClawError
from sjtuclaw.infrastructure.runtime import UuidIdGenerator
from sjtuclaw.models.compaction import CompactionResult
from sjtuclaw.models.runtime import (
    InvocationSource,
    RuntimeEventType,
    RuntimeRequest,
    UserInput,
)
from sjtuclaw.models.skill import SkillReference
from sjtuclaw.runtime import CommandService, MemoryService, SessionService, SkillService


@dataclass(slots=True)
class CLIState:
    session_id: str
    queued_skill_task: str | None = None
    queued_skill_refs: tuple[SkillReference, ...] = ()


class CompactionController(Protocol):
    async def compact_session(
        self,
        session_id: str,
        force: bool = True,
    ) -> CompactionResult: ...


def _load_prompt(name: str) -> str:
    resource = files("sjtuclaw.prompts").joinpath(name)
    content = resource.read_text(encoding="utf-8").strip()
    if not content:
        raise ConfigurationError(f"packaged prompt is empty: {name}")
    return content


async def _handle_command(
    raw: str,
    sessions: SessionService,
    state: CLIState,
    output_fn: Callable[[str], None],
    compaction: CompactionController | None = None,
    skills: SkillService | None = None,
    memory: MemoryService | None = None,
    commands: CommandService | None = None,
) -> bool:
    """Adapt shared command outcomes to the CLI's selected Session and Run loop."""

    command_service = commands or CommandService(
        sessions=sessions,
        memories=memory,
        compaction=compaction,
        skills=skills,
    )
    outcome = await command_service.execute(raw, current_session_id=state.session_id)
    if outcome is None:
        return False
    for line in outcome.lines:
        output_fn(line)
    if outcome.effects.select_session_id is not None:
        state.session_id = outcome.effects.select_session_id
    if outcome.submission is not None:
        state.queued_skill_task = outcome.submission.text
        state.queued_skill_refs = outcome.submission.skill_refs
    return True


async def run_cli(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    settings = Settings.load()
    ids = UuidIdGenerator()
    application = build_cli_application(
        settings,
        _load_prompt("system_prompt.md"),
        _load_prompt("soul.md"),
        _load_prompt("compact_prompt.md"),
    )
    state = CLIState(application.initial_session_id)
    recover_pending = getattr(application.runtime, "recover_pending", None)
    if callable(recover_pending):
        await recover_pending()

    output_fn(f"SJTUClaw started. Session: {state.session_id}. Type /exit to quit.")
    try:
        while True:
            try:
                raw = input_fn("User> ").strip()
            except EOFError:
                break
            if not raw:
                continue
            if raw == "/exit":
                break
            try:
                if await _handle_command(
                    raw,
                    application.session_service,
                    state,
                    output_fn,
                    application.compaction,
                    getattr(application, "skills", None),
                    getattr(application, "memory_service", None),
                    getattr(application, "commands", None),
                ):
                    if state.queued_skill_task is None:
                        continue
                    raw = state.queued_skill_task
                    skill_refs = state.queued_skill_refs
                    state.queued_skill_task = None
                    state.queued_skill_refs = ()
                else:
                    skill_refs = ()
            except (SJTUClawError, ValueError) as exc:
                output_fn(f"Error> {type(exc).__name__}: {exc}")
                continue

            request = RuntimeRequest(
                request_id=ids.new_id("request"),
                session_id=state.session_id,
                source=InvocationSource.CLI,
                user_input=UserInput(raw),
                skill_refs=skill_refs,
            )
            handle = await application.runtime.submit(request)
            async for event in handle.events():
                if event.event_type is RuntimeEventType.ASSISTANT_MESSAGE:
                    output_fn(f"Assistant> {event.payload['content']}")
                elif event.event_type is RuntimeEventType.TOOL_CALL:
                    output_fn(
                        "Tool call> "
                        f"{event.payload['name']} "
                        f"{event.payload['arguments']}"
                    )
                elif event.event_type is RuntimeEventType.TOOL_RESULT:
                    suffix = (
                        ""
                        if event.payload["error_code"] is None
                        else f" ({event.payload['error_code']})"
                    )
                    output_fn(
                        "Tool result> "
                        f"{event.payload['tool']} {event.payload['status']}{suffix}: "
                        f"{event.payload['content']}"
                    )
                elif event.event_type is RuntimeEventType.COMPACTION_COMPLETED:
                    output_fn(
                        "Compaction> 自动完成："
                        f"{event.payload['before_tokens']} -> "
                        f"{event.payload['after_tokens']} tokens，"
                        f"counter={event.payload['token_counter_mode']}，"
                        "summary_prompt_tokens="
                        f"{event.payload['summary_prompt_tokens']}"
                    )
                elif event.event_type is RuntimeEventType.COMPACTION_FAILED:
                    output_fn(
                        f"Compaction warning> {event.payload['error_type']}: "
                        f"{event.payload['message']}"
                    )
                elif event.event_type is RuntimeEventType.RUN_FAILED:
                    output_fn(
                        f"Error> {event.payload['error_type']}: {event.payload['message']}"
                    )
                elif event.event_type is RuntimeEventType.RUN_CANCELLED:
                    output_fn("Cancelled.")
    finally:
        await application.aclose()


def main() -> None:
    try:
        asyncio.run(run_cli())
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
    except SJTUClawError as exc:
        print(f"Startup error: {type(exc).__name__}: {exc}")
    except KeyboardInterrupt:
        print("\nCancelled.")
