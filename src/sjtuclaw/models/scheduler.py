"""Persistent scheduler domain contracts, separate from Session JSON."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sjtuclaw.models.skill import SkillReference

SCHEDULER_MIN_INTERVAL_SECONDS = 300


class ScheduleKind(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    DAILY = "daily"
    WEEKLY = "weekly"


class TaskStatus(StrEnum):
    ACTIVE = "active"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskArchiveReason(StrEnum):
    USER_CANCELLED = "user_cancelled"
    SESSION_ARCHIVED = "session_archived"


class OccurrenceStatus(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    SKIPPED_OVERLAP = "skipped_overlap"


_ACTIVE_OCCURRENCE_STATUSES = frozenset(
    {OccurrenceStatus.PLANNED, OccurrenceStatus.QUEUED, OccurrenceStatus.RUNNING}
)
_MANAGED_PERMISSIONS = frozenset({"workspace_write", "shell", "download"})


@dataclass(frozen=True, slots=True)
class Schedule:
    """A rule keeps its UTC anchor plus only the calendar information it needs."""

    kind: ScheduleKind
    anchor_at: datetime
    interval_seconds: int | None = None
    timezone: str | None = None
    hour: int | None = None
    minute: int | None = None
    weekdays: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _require_utc(self.anchor_at, "schedule anchor_at")
        if self.kind is ScheduleKind.ONCE:
            if any(
                value is not None
                for value in (self.interval_seconds, self.timezone, self.hour, self.minute)
            ) or self.weekdays:
                raise ValueError("once schedule only accepts anchor_at")
            return
        if self.kind is ScheduleKind.INTERVAL:
            if self.interval_seconds is None or self.interval_seconds < SCHEDULER_MIN_INTERVAL_SECONDS:
                raise ValueError("interval schedule is below the minimum interval")
            if any(value is not None for value in (self.timezone, self.hour, self.minute)) or self.weekdays:
                raise ValueError("interval schedule only accepts anchor_at and interval_seconds")
            return
        if self.interval_seconds is not None:
            raise ValueError("calendar schedule cannot have interval_seconds")
        if self.timezone is None or self.hour is None or self.minute is None:
            raise ValueError("calendar schedule requires timezone, hour and minute")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("calendar schedule timezone is invalid") from exc
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise ValueError("calendar schedule local time is invalid")
        if self.kind is ScheduleKind.DAILY:
            if self.weekdays:
                raise ValueError("daily schedule cannot have weekdays")
            return
        if not self.weekdays or any(day < 0 or day > 6 for day in self.weekdays):
            raise ValueError("weekly schedule requires weekdays in ISO weekday range")
        if len(self.weekdays) != len(set(self.weekdays)):
            raise ValueError("weekly schedule weekdays must be unique")


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    session_id: str
    instruction: str
    schedule: Schedule
    status: TaskStatus
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime
    workspace_binding_revision: int | None = None
    managed_permissions: tuple[str, ...] = ()
    skill_refs: tuple[SkillReference, ...] = ()
    archived_at: datetime | None = None
    archived_reason: TaskArchiveReason | None = None

    def __post_init__(self) -> None:
        if not self.task_id or not self.session_id or not self.instruction.strip():
            raise ValueError("task identifiers and instruction must not be empty")
        _require_utc(self.created_at, "task created_at")
        _require_utc(self.updated_at, "task updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("task updated_at must not precede created_at")
        if self.next_run_at is not None:
            _require_utc(self.next_run_at, "task next_run_at")
        if self.status is TaskStatus.ACTIVE and self.next_run_at is None:
            raise ValueError("active task requires next_run_at")
        if self.status is not TaskStatus.ACTIVE and self.next_run_at is not None:
            raise ValueError("inactive task cannot retain next_run_at")
        if (self.archived_at is None) != (self.archived_reason is None):
            raise ValueError("task archive fields must be supplied together")
        if self.archived_at is not None:
            _require_utc(self.archived_at, "task archived_at")
            if self.status is TaskStatus.ACTIVE:
                raise ValueError("archived task cannot be active")
        if self.workspace_binding_revision is not None and self.workspace_binding_revision < 1:
            raise ValueError("workspace_binding_revision must be positive")
        if any(permission not in _MANAGED_PERMISSIONS for permission in self.managed_permissions):
            raise ValueError("task contains an unknown managed permission")
        if len(self.managed_permissions) != len(set(self.managed_permissions)):
            raise ValueError("managed permissions must be unique")
        if len(self.skill_refs) > 3:
            raise ValueError("task skill refs must contain at most three values")
        skill_ids = [reference.skill_id for reference in self.skill_refs]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("task skill refs must have distinct skill IDs")


@dataclass(frozen=True, slots=True)
class Occurrence:
    occurrence_id: str
    task_id: str
    sequence_number: int
    status: OccurrenceStatus
    scheduled_for: datetime
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None = None
    run_id: str | None = None
    request_id: str | None = None
    assistant_message_id: str | None = None
    coalesced: bool = False
    missed_count: int = 0
    retry_of_occurrence_id: str | None = None
    blocking_occurrence_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.occurrence_id or not self.task_id or self.sequence_number < 1:
            raise ValueError("occurrence identifiers and sequence must be valid")
        for label, value in (
            ("scheduled_for", self.scheduled_for),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            _require_utc(value, f"occurrence {label}")
        if self.updated_at < self.created_at:
            raise ValueError("occurrence updated_at must not precede created_at")
        if self.queued_at is not None:
            _require_utc(self.queued_at, "occurrence queued_at")
        if self.missed_count < 0:
            raise ValueError("occurrence missed_count must not be negative")
        if self.coalesced != (self.missed_count > 0):
            raise ValueError("coalesced occurrence must record missed_count")
        if self.status in {OccurrenceStatus.QUEUED, OccurrenceStatus.RUNNING, OccurrenceStatus.COMPLETED, OccurrenceStatus.FAILED}:
            if self.queued_at is None or not self.request_id:
                raise ValueError("submitted occurrence requires queued_at and request_id")
        if self.status is OccurrenceStatus.RUNNING and not self.run_id:
            raise ValueError("running occurrence requires run_id")
        if self.status is OccurrenceStatus.COMPLETED and not self.assistant_message_id:
            raise ValueError("completed occurrence requires assistant_message_id")
        if self.status is OccurrenceStatus.SKIPPED_OVERLAP and not self.blocking_occurrence_id:
            raise ValueError("overlap occurrence requires blocking_occurrence_id")
        if self.status is OccurrenceStatus.FAILED and not self.error_code:
            raise ValueError("failed occurrence requires error_code")

    @property
    def is_active(self) -> bool:
        return self.status in _ACTIVE_OCCURRENCE_STATUSES


def _require_utc(value: datetime, context: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{context} must be UTC")


def next_run_after(schedule: Schedule, after: datetime) -> datetime | None:
    """Calculate from the immutable rule anchor, never from a run completion time."""

    _require_utc(after, "scheduler reference time")
    if schedule.kind is ScheduleKind.ONCE:
        return schedule.anchor_at if schedule.anchor_at > after else None
    if schedule.kind is ScheduleKind.INTERVAL:
        if after < schedule.anchor_at:
            return schedule.anchor_at
        assert schedule.interval_seconds is not None
        elapsed_seconds = (after - schedule.anchor_at).total_seconds()
        intervals = int(elapsed_seconds // schedule.interval_seconds) + 1
        return schedule.anchor_at + timedelta(seconds=intervals * schedule.interval_seconds)

    assert schedule.timezone is not None
    assert schedule.hour is not None
    assert schedule.minute is not None
    timezone = ZoneInfo(schedule.timezone)
    local_after = after.astimezone(timezone)
    local_anchor = schedule.anchor_at.astimezone(timezone)
    candidate_date = max(local_after.date(), local_anchor.date())
    for _ in range(3_660):
        candidate = datetime.combine(
            candidate_date,
            time(schedule.hour, schedule.minute),
            tzinfo=timezone,
        ).astimezone(UTC)
        if (
            candidate >= schedule.anchor_at
            and candidate > after
            and (
                schedule.kind is ScheduleKind.DAILY
                or candidate_date.weekday() in schedule.weekdays
            )
        ):
            return candidate
        candidate_date += timedelta(days=1)
    raise ValueError("calendar schedule has no future occurrence within ten years")
