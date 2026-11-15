"""Unified invocation and event contracts for all entrypoints."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from sjtuclaw.models.skill import SkillReference


class InvocationSource(StrEnum):
    CLI = "cli"
    GATEWAY = "gateway"
    SCHEDULER = "scheduler"


@dataclass(frozen=True, slots=True)
class UserInput:
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("user input must not be empty")


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    request_id: str
    session_id: str
    source: InvocationSource
    user_input: UserInput
    actor_id: str | None = None
    attachment_refs: tuple[str, ...] = ()
    idempotency_key: str | None = None
    scheduler_task_id: str | None = None
    occurrence_id: str | None = None
    workspace_binding_revision: int | None = None
    managed_permission_categories: tuple[str, ...] = ()
    skill_refs: tuple[SkillReference, ...] = ()
    automatic_skill_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must not be empty")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if self.source is not InvocationSource.SCHEDULER and any(
            value is not None
            for value in (
                self.scheduler_task_id,
                self.occurrence_id,
                self.workspace_binding_revision,
            )
        ):
            raise ValueError("managed scheduler fields require a scheduler request")
        if self.workspace_binding_revision is not None and self.workspace_binding_revision < 1:
            raise ValueError("workspace_binding_revision must be positive")
        if len(self.managed_permission_categories) != len(
            set(self.managed_permission_categories)
        ) or any(not category for category in self.managed_permission_categories):
            raise ValueError("managed permission categories must be unique and non-empty")
        if len(self.skill_refs) > 3:
            raise ValueError("a runtime request may select at most three skills")
        skill_ids = [reference.skill_id for reference in self.skill_refs]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("runtime request skill references must have distinct skill IDs")
        if len(self.automatic_skill_ids) != len(set(self.automatic_skill_ids)):
            raise ValueError("automatic skill ids must be unique")
        if any(skill_id not in set(skill_ids) for skill_id in self.automatic_skill_ids):
            raise ValueError("automatic skill ids must be selected skill ids")


class RuntimeEventType(StrEnum):
    REQUEST_ACCEPTED = "request_accepted"
    REQUEST_QUEUED = "request_queued"
    RUN_STARTED = "run_started"
    RUN_PROGRESS = "run_progress"
    ASSISTANT_DELTA = "assistant_delta"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESUMED = "approval_resumed"
    COMPACTION_COMPLETED = "compaction_completed"
    COMPACTION_FAILED = "compaction_failed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    STEER_ACCEPTED = "steer_accepted"
    STEER_WAITING_SAFE_POINT = "steer_waiting_safe_point"
    STEER_APPLIED = "steer_applied"
    STEER_REJECTED = "steer_rejected"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_type: RuntimeEventType
    session_id: str
    invocation_id: str
    sequence_number: int
    source: InvocationSource
    payload: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.sequence_number < 0:
            raise ValueError("sequence_number must not be negative")
