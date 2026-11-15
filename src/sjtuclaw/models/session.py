"""Persistent session model."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from sjtuclaw.models.message import Message
from sjtuclaw.models.run_record import RunRecord
from sjtuclaw.models.runtime import InvocationSource
from sjtuclaw.models.skill import SkillReference, SkillUsage
from sjtuclaw.models.tool import ToolCall, ToolIntent


class RunStatus(StrEnum):
    """Durable lifecycle state for one accepted runtime request."""

    QUEUED = "queued"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class SessionArchiveReason(StrEnum):
    USER_ARCHIVED = "user_archived"


class ApprovalDecisionStatus(StrEnum):
    """Persisted terminal choice for one approval checkpoint."""

    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"


def _require_identifier(value: str, field: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{field} must not be empty or padded")


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")


def _validate_denial_reason(reason: str | None) -> None:
    if reason is None:
        return
    if reason != reason.strip() or len(reason) > 1_000:
        raise ValueError("approval denial reason must be trimmed and at most 1000 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in reason):
        raise ValueError("approval denial reason must not contain control characters")


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """Trusted Workspace facts attached to exactly one Session."""

    binding_id: str
    canonical_root: str
    display_path: str
    revision: int
    bound_at: datetime

    def __post_init__(self) -> None:
        _require_identifier(self.binding_id, "workspace binding_id")
        if not self.canonical_root.strip() or not self.display_path.strip():
            raise ValueError("workspace paths must not be empty")
        if self.revision < 1:
            raise ValueError("workspace revision must be positive")
        _require_utc(self.bound_at, "workspace bound_at")


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Immutable, idempotent answer to a pending approval."""

    decision_id: str
    approval_id: str
    status: ApprovalDecisionStatus
    idempotency_key: str
    actor_id: str
    decided_at: datetime
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.decision_id, "approval decision_id")
        _require_identifier(self.approval_id, "approval decision approval_id")
        _require_identifier(self.idempotency_key, "approval decision idempotency_key")
        _require_identifier(self.actor_id, "approval decision actor_id")
        _require_utc(self.decided_at, "approval decision decided_at")
        if self.status is ApprovalDecisionStatus.APPROVED and self.reason is not None:
            raise ValueError("approved decisions cannot include a reason")
        if self.status is ApprovalDecisionStatus.TIMED_OUT:
            if self.actor_id != "system_timeout" or self.reason is not None:
                raise ValueError("timed out decisions require the system timeout actor only")
        _validate_denial_reason(self.reason)


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """Recoverable approval checkpoint containing only server-trusted intent."""

    approval_id: str
    run_id: str
    invocation_id: str
    tool_call: ToolCall
    intent: ToolIntent
    reason: str
    created_at: datetime
    expires_at: datetime
    workspace_binding_id: str | None = None
    workspace_revision: int | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.approval_id, "pending approval_id")
        _require_identifier(self.run_id, "pending approval run_id")
        _require_identifier(self.invocation_id, "pending approval invocation_id")
        if self.tool_call.call_id != self.intent.call_id:
            raise ValueError("pending approval tool call and intent IDs must match")
        if self.tool_call.name != self.intent.name:
            raise ValueError("pending approval tool call and intent names must match")
        if not self.reason.strip():
            raise ValueError("pending approval reason must not be empty")
        _require_utc(self.created_at, "pending approval created_at")
        _require_utc(self.expires_at, "pending approval expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("pending approval expires_at must follow created_at")
        if self.workspace_binding_id is not None:
            _require_identifier(self.workspace_binding_id, "pending approval workspace_binding_id")
        if self.workspace_revision is not None and self.workspace_revision < 1:
            raise ValueError("pending approval workspace_revision must be positive")
        if (self.workspace_binding_id is None) != (self.workspace_revision is None):
            raise ValueError("pending approval workspace binding and revision must be supplied together")


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    """Minimal persisted position from which an approval-paused run can resume."""

    checkpoint_id: str
    run_id: str
    request_id: str
    pending_approval_id: str
    next_tool_call_index: int
    created_at: datetime
    source: InvocationSource
    actor_id: str | None
    input_text: str
    attachment_refs: tuple[str, ...] = ()
    approved_execution_started: bool = False
    skill_refs: tuple[SkillReference, ...] = ()
    automatic_skill_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.checkpoint_id, "run checkpoint_id")
        _require_identifier(self.run_id, "run checkpoint run_id")
        _require_identifier(self.request_id, "run checkpoint request_id")
        _require_identifier(self.pending_approval_id, "run checkpoint pending_approval_id")
        if self.next_tool_call_index < 0:
            raise ValueError("run checkpoint next_tool_call_index must not be negative")
        _require_utc(self.created_at, "run checkpoint created_at")
        if not self.input_text.strip():
            raise ValueError("run checkpoint input_text must not be empty")
        if any(not reference for reference in self.attachment_refs):
            raise ValueError("run checkpoint attachment refs must not be empty")
        if len(self.attachment_refs) != len(set(self.attachment_refs)):
            raise ValueError("run checkpoint attachment refs must be unique")
        if type(self.approved_execution_started) is not bool:
            raise TypeError("run checkpoint approved_execution_started must be a bool")
        _validate_skill_refs(self.skill_refs, "run checkpoint")
        _validate_automatic_skill_ids(self.automatic_skill_ids, self.skill_refs)


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """Accepted work that has not yet atomically become a user message."""

    request_id: str
    acceptance_sequence: int
    source: InvocationSource
    actor_id: str | None
    text: str
    submitted_at: datetime
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
            raise ValueError("pending request_id must not be empty")
        if self.acceptance_sequence < 1:
            raise ValueError("acceptance_sequence must be positive")
        if not self.text.strip():
            raise ValueError("pending request text must not be empty")
        if self.submitted_at.tzinfo is None or self.submitted_at.utcoffset() is None:
            raise ValueError("pending request timestamp must be timezone-aware")
        if any(not reference for reference in self.attachment_refs):
            raise ValueError("attachment refs must not be empty")
        if len(self.attachment_refs) != len(set(self.attachment_refs)):
            raise ValueError("attachment refs must be unique")
        if self.idempotency_key is not None and not self.idempotency_key:
            raise ValueError("idempotency_key must not be empty when supplied")
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
        _validate_skill_refs(self.skill_refs, "pending request")
        _validate_automatic_skill_ids(self.automatic_skill_ids, self.skill_refs)


@dataclass(frozen=True, slots=True)
class RunState:
    """Small persisted run summary; message bodies remain in Session.messages."""

    run_id: str
    request_id: str
    status: RunStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    assistant_message_id: str | None = None
    idempotency_key: str | None = None
    checkpoint: RunCheckpoint | None = None
    selected_skill_refs: tuple[SkillReference, ...] = ()

    def __post_init__(self) -> None:
        if not self.run_id or not self.request_id:
            raise ValueError("run identifiers must not be empty")
        for timestamp in (self.started_at, self.completed_at):
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError("run timestamps must be timezone-aware")
        if self.started_at is not None and self.completed_at is not None:
            if self.completed_at < self.started_at:
                raise ValueError("completed_at must not precede started_at")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        if self.assistant_message_id is not None and not self.assistant_message_id:
            raise ValueError("assistant_message_id must not be empty when supplied")
        if self.idempotency_key is not None and not self.idempotency_key:
            raise ValueError("idempotency_key must not be empty when supplied")
        if self.checkpoint is not None:
            if self.checkpoint.run_id != self.run_id:
                raise ValueError("run checkpoint run_id must match its run")
            if self.checkpoint.request_id != self.request_id:
                raise ValueError("run checkpoint request_id must match its run")
            if self.status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED_BEFORE_START,
                RunStatus.CANCELLED,
                RunStatus.INTERRUPTED,
            }:
                raise ValueError("terminal runs cannot retain a checkpoint")
        _validate_skill_refs(self.selected_skill_refs, "run")
        if self.status is RunStatus.COMPLETED:
            if (
                self.started_at is None
                or self.completed_at is None
                or self.duration_ms is None
                or self.assistant_message_id is None
            ):
                raise ValueError("completed run requires timestamps, duration and assistant message")
        elif self.assistant_message_id is not None:
            raise ValueError("only completed runs may reference assistant messages")


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    title: str
    messages: tuple[Message, ...]
    summary: str
    created_at: datetime
    updated_at: datetime
    next_sequence_number: int = 1
    run_records: tuple[RunRecord, ...] = ()
    pending_requests: tuple[PendingRequest, ...] = ()
    runs: tuple[RunState, ...] = ()
    workspace_binding: WorkspaceBinding | None = None
    pending_approval: PendingApproval | None = None
    approval_decisions: tuple[ApprovalDecision, ...] = ()
    explicit_skill_refs: tuple[SkillReference, ...] = ()
    skill_usages: tuple[SkillUsage, ...] = ()
    prompt_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    completion_tokens: int = 0
    cache_breakdown_complete: bool = True
    token_tracking_started_at: datetime | None = None
    archived_at: datetime | None = None
    archived_reason: SessionArchiveReason | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("session timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.next_sequence_number < 1:
            raise ValueError("next_sequence_number must be positive")
        token_counts = (
            self.prompt_tokens,
            self.prompt_cache_hit_tokens,
            self.prompt_cache_miss_tokens,
            self.completion_tokens,
        )
        if min(token_counts) < 0:
            raise ValueError("session token counts must not be negative")
        if (
            self.cache_breakdown_complete
            and self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
            != self.prompt_tokens
        ):
            raise ValueError("complete session cache counts must equal prompt_tokens")
        if type(self.cache_breakdown_complete) is not bool:
            raise TypeError("cache_breakdown_complete must be a bool")
        if self.token_tracking_started_at is not None:
            _require_utc(self.token_tracking_started_at, "token_tracking_started_at")
        if (self.archived_at is None) != (self.archived_reason is None):
            raise ValueError("session archive fields must be supplied together")
        if self.archived_at is not None:
            _require_utc(self.archived_at, "session archived_at")

        message_ids = [message.message_id for message in self.messages]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("message_id values must be unique within a session")

        sequence_numbers = [message.sequence_number for message in self.messages]
        if any(current <= previous for previous, current in pairwise(sequence_numbers)):
            raise ValueError("message sequence_number values must be strictly increasing")
        if sequence_numbers and self.next_sequence_number <= sequence_numbers[-1]:
            raise ValueError("next_sequence_number must be greater than all message sequence numbers")

        record_ids = [record.record_id for record in self.run_records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("record_id values must be unique within a session")
        record_sequences = [record.sequence_number for record in self.run_records]
        if any(current <= previous for previous, current in pairwise(record_sequences)):
            raise ValueError("run record sequence_number values must be strictly increasing")

        pending_ids = [request.request_id for request in self.pending_requests]
        if len(pending_ids) != len(set(pending_ids)):
            raise ValueError("pending request_id values must be unique within a session")
        run_ids = [run.run_id for run in self.runs]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("run_id values must be unique within a session")

        decision_ids = [decision.decision_id for decision in self.approval_decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("approval decision_id values must be unique within a session")
        decision_approval_ids = [
            decision.approval_id for decision in self.approval_decisions
        ]
        if len(decision_approval_ids) != len(set(decision_approval_ids)):
            raise ValueError("approval decisions must be unique per approval")
        decision_idempotency_keys = [
            decision.idempotency_key for decision in self.approval_decisions
        ]
        if len(decision_idempotency_keys) != len(set(decision_idempotency_keys)):
            raise ValueError("approval decision idempotency keys must be unique")

        if self.pending_approval is not None:
            pending = self.pending_approval
            run = next((run for run in self.runs if run.run_id == pending.run_id), None)
            if run is None:
                raise ValueError("pending approval run must exist in the session")
            if run.checkpoint is None:
                raise ValueError("pending approval run requires a checkpoint")
            if run.checkpoint.pending_approval_id != pending.approval_id:
                raise ValueError("pending approval must match its run checkpoint")
            if self.workspace_binding is None:
                if pending.workspace_binding_id is not None:
                    raise ValueError("pending approval cannot reference an absent workspace")
            elif (
                pending.workspace_binding_id != self.workspace_binding.binding_id
                or pending.workspace_revision != self.workspace_binding.revision
            ):
                raise ValueError("pending approval workspace must match the current binding")
        _validate_skill_refs(self.explicit_skill_refs, "session explicit")
        usage_keys = [(usage.request_id, usage.selection_order) for usage in self.skill_usages]
        if len(usage_keys) != len(set(usage_keys)):
            raise ValueError("skill usage selection order must be unique within a request")


def _validate_skill_refs(references: tuple[SkillReference, ...], context: str) -> None:
    if len(references) > 3:
        raise ValueError(f"{context} may select at most three skills")
    skill_ids = [reference.skill_id for reference in references]
    if len(skill_ids) != len(set(skill_ids)):
        raise ValueError(f"{context} skill references must have distinct skill IDs")


def _validate_automatic_skill_ids(
    automatic_skill_ids: tuple[str, ...], references: tuple[SkillReference, ...]
) -> None:
    if len(automatic_skill_ids) != len(set(automatic_skill_ids)):
        raise ValueError("automatic skill ids must be unique")
    selected_skill_ids = {reference.skill_id for reference in references}
    if any(skill_id not in selected_skill_ids for skill_id in automatic_skill_ids):
        raise ValueError("automatic skill ids must be selected skill ids")
