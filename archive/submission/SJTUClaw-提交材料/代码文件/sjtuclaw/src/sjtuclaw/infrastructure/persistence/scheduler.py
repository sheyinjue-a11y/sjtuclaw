"""Strict atomic JSON TaskStore kept outside Session persistence."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sjtuclaw.errors import StoreError
from sjtuclaw.models.scheduler import (
    Occurrence,
    OccurrenceStatus,
    Schedule,
    ScheduleKind,
    Task,
    TaskArchiveReason,
    TaskStatus,
)
from sjtuclaw.models.skill import SkillReference

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class JsonTaskStore:
    def __init__(self, root: Path) -> None:
        self._root = root / "scheduler"

    def create_task(self, task: Task) -> None:
        path = self._task_path(task.task_id)
        if path.exists():
            raise StoreError("task already exists")
        _write(path, _task_to_json(task))

    def load_task(self, task_id: str) -> Task:
        return _task_from_json(_read(self._task_path(task_id)))

    def list_tasks(self, *, archived: bool = False) -> tuple[Task, ...]:
        directory = self._root / "tasks"
        if not directory.exists():
            return ()
        try:
            return tuple(
                sorted(
                    (
                        task
                        for path in directory.glob("*.json")
                        if ((task := _task_from_json(_read(path))).archived_at is not None)
                        == archived
                    ),
                    key=lambda task: task.created_at,
                )
            )
        except OSError as exc:
            raise StoreError("failed to list tasks") from exc

    def save_task(self, task: Task) -> None:
        path = self._task_path(task.task_id)
        self.load_task(task.task_id)
        _write(path, _task_to_json(task))

    def delete_task(self, task_id: str) -> None:
        path = self._task_path(task_id)
        occurrence_directory = self._root / "occurrences" / task_id
        if path.exists():
            task = self.load_task(task_id)
            if task.archived_at is None:
                raise StoreError("task is not archived")
        try:
            if occurrence_directory.exists():
                shutil.rmtree(occurrence_directory)
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StoreError("failed to delete task") from exc

    def create_occurrence(self, occurrence: Occurrence) -> None:
        path = self._occurrence_path(
            occurrence.task_id, occurrence.sequence_number, occurrence.occurrence_id
        )
        if path.exists():
            raise StoreError("occurrence already exists")
        _write(path, _occurrence_to_json(occurrence))

    def load_occurrence(self, task_id: str, occurrence_id: str) -> Occurrence:
        for path in self._occurrence_paths(task_id):
            occurrence = _occurrence_from_json(_read(path))
            if occurrence.occurrence_id == occurrence_id:
                return occurrence
        raise StoreError("occurrence not found")

    def save_occurrence(self, occurrence: Occurrence) -> None:
        path = self._occurrence_path(
            occurrence.task_id, occurrence.sequence_number, occurrence.occurrence_id
        )
        _occurrence_from_json(_read(path))
        _write(path, _occurrence_to_json(occurrence))

    def list_occurrences(
        self, task_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[tuple[Occurrence, ...], str | None]:
        if not 1 <= limit <= 100:
            raise StoreError("occurrence page limit is invalid")
        values = tuple(
            sorted(
                (_occurrence_from_json(_read(path)) for path in self._occurrence_paths(task_id)),
                key=lambda item: item.sequence_number,
            )
        )
        offset = _cursor(cursor)
        page = values[offset : offset + limit]
        next_cursor = (
            _encode_cursor(offset + len(page)) if offset + len(page) < len(values) else None
        )
        return page, next_cursor

    def _task_path(self, task_id: str) -> Path:
        _id(task_id)
        return self._root / "tasks" / f"{task_id}.json"

    def _occurrence_paths(self, task_id: str) -> tuple[Path, ...]:
        _id(task_id)
        directory = self._root / "occurrences" / task_id
        return () if not directory.exists() else tuple(sorted(directory.glob("*.json")))

    def _occurrence_path(self, task_id: str, sequence: int, occurrence_id: str) -> Path:
        _id(task_id)
        _id(occurrence_id)
        if sequence < 1:
            raise StoreError("occurrence sequence is invalid")
        return self._root / "occurrences" / task_id / f"{sequence:020d}-{occurrence_id}.json"


def _task_to_json(task: Task) -> dict[str, object]:
    return {
        "schemaVersion": 3,
        "task": {
            "taskId": task.task_id,
            "sessionId": task.session_id,
            "instruction": task.instruction,
            "schedule": _schedule_to_json(task.schedule),
            "status": task.status.value,
            "nextRunAt": _time_or_none(task.next_run_at),
            "createdAt": task.created_at.isoformat(),
            "updatedAt": task.updated_at.isoformat(),
            "workspaceBindingRevision": task.workspace_binding_revision,
            "managedPermissions": list(task.managed_permissions),
            "skillRefs": [_skill_reference_to_json(reference) for reference in task.skill_refs],
            "archivedAt": _time_or_none(task.archived_at),
            "archivedReason": (
                None if task.archived_reason is None else task.archived_reason.value
            ),
        },
    }


def _task_from_json(value: object) -> Task:
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "task"}
        or value["schemaVersion"] not in {1, 2, 3}
        or not isinstance(value["task"], dict)
    ):
        raise StoreError("scheduler document is invalid")
    schema_version = value["schemaVersion"]
    data = cast(dict[str, object], value["task"])
    fields = {
        "taskId", "sessionId", "instruction", "schedule", "status", "nextRunAt",
        "createdAt", "updatedAt", "workspaceBindingRevision", "managedPermissions",
        "skillRefs",
    }
    if schema_version == 3:
        fields |= {"archivedAt", "archivedReason"}
    _fields(data, fields)
    return Task(
        _string(data, "taskId"),
        _string(data, "sessionId"),
        _string(data, "instruction"),
        _schedule_from_json(data["schedule"]),
        TaskStatus(_string(data, "status")),
        _optional_time(data["nextRunAt"]),
        _time(data["createdAt"]),
        _time(data["updatedAt"]),
        _optional_int(data["workspaceBindingRevision"]),
        tuple(_strings(data["managedPermissions"])),
        (
            _legacy_skill_refs(data["skillRefs"])
            if schema_version == 1
            else tuple(_skill_reference_from_json(item) for item in _list(data["skillRefs"]))
        ),
        None if schema_version < 3 else _optional_time(data["archivedAt"]),
        (
            None
            if schema_version < 3 or data["archivedReason"] is None
            else TaskArchiveReason(_string(data, "archivedReason"))
        ),
    )


def _skill_reference_to_json(reference: SkillReference) -> dict[str, str]:
    return {
        "skillId": reference.skill_id,
        "snapshotId": reference.snapshot_id,
        "snapshotHash": reference.snapshot_hash,
        "version": reference.version,
    }


def _skill_reference_from_json(value: object) -> SkillReference:
    if not isinstance(value, dict) or set(value) != {
        "skillId",
        "snapshotId",
        "snapshotHash",
        "version",
    }:
        raise StoreError("scheduler skill reference is invalid")
    try:
        return SkillReference(
            skill_id=_string(value, "skillId"),
            snapshot_id=_string(value, "snapshotId"),
            snapshot_hash=_string(value, "snapshotHash"),
            version=_string(value, "version"),
        )
    except ValueError as exc:
        raise StoreError("scheduler skill reference is invalid") from exc


def _legacy_skill_refs(value: object) -> tuple[SkillReference, ...]:
    if _strings(value):
        raise StoreError("legacy task skill references require re-creation")
    return ()


def _occurrence_to_json(item: Occurrence) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "occurrence": {
            "occurrenceId": item.occurrence_id,
            "taskId": item.task_id,
            "sequenceNumber": item.sequence_number,
            "status": item.status.value,
            "scheduledFor": item.scheduled_for.isoformat(),
            "createdAt": item.created_at.isoformat(),
            "updatedAt": item.updated_at.isoformat(),
            "queuedAt": _time_or_none(item.queued_at),
            "runId": item.run_id,
            "requestId": item.request_id,
            "assistantMessageId": item.assistant_message_id,
            "coalesced": item.coalesced,
            "missedCount": item.missed_count,
            "retryOfOccurrenceId": item.retry_of_occurrence_id,
            "blockingOccurrenceId": item.blocking_occurrence_id,
            "errorCode": item.error_code,
        },
    }


def _occurrence_from_json(value: object) -> Occurrence:
    data = _document(value, "occurrence")
    _fields(
        data,
        {
            "occurrenceId",
            "taskId",
            "sequenceNumber",
            "status",
            "scheduledFor",
            "createdAt",
            "updatedAt",
            "queuedAt",
            "runId",
            "requestId",
            "assistantMessageId",
            "coalesced",
            "missedCount",
            "retryOfOccurrenceId",
            "blockingOccurrenceId",
            "errorCode",
        },
    )
    return Occurrence(
        _string(data, "occurrenceId"),
        _string(data, "taskId"),
        _int(data, "sequenceNumber"),
        OccurrenceStatus(_string(data, "status")),
        _time(data["scheduledFor"]),
        _time(data["createdAt"]),
        _time(data["updatedAt"]),
        _optional_time(data["queuedAt"]),
        _optional_string(data["runId"]),
        _optional_string(data["requestId"]),
        _optional_string(data["assistantMessageId"]),
        _bool(data, "coalesced"),
        _int(data, "missedCount"),
        _optional_string(data["retryOfOccurrenceId"]),
        _optional_string(data["blockingOccurrenceId"]),
        _optional_string(data["errorCode"]),
    )


def _schedule_to_json(schedule: Schedule) -> dict[str, object]:
    return {
        "kind": schedule.kind.value,
        "anchorAt": schedule.anchor_at.isoformat(),
        "intervalSeconds": schedule.interval_seconds,
        "timezone": schedule.timezone,
        "hour": schedule.hour,
        "minute": schedule.minute,
        "weekdays": list(schedule.weekdays),
    }


def _schedule_from_json(value: object) -> Schedule:
    if not isinstance(value, dict):
        raise StoreError("schedule is invalid")
    _fields(
        value, {"kind", "anchorAt", "intervalSeconds", "timezone", "hour", "minute", "weekdays"}
    )
    return Schedule(
        ScheduleKind(_string(value, "kind")),
        _time(value["anchorAt"]),
        _optional_int(value["intervalSeconds"]),
        _optional_string(value["timezone"]),
        _optional_int(value["hour"]),
        _optional_int(value["minute"]),
        tuple(_ints(value["weekdays"])),
    )


def _document(value: object, key: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", key}
        or value["schemaVersion"] != 1
        or not isinstance(value[key], dict)
    ):
        raise StoreError("scheduler document is invalid")
    return cast(dict[str, object], value[key])


def _fields(data: dict[str, object], expected: set[str]) -> None:
    if set(data) != expected:
        raise StoreError("scheduler document fields are invalid")


def _string(data: dict[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise StoreError("scheduler value is invalid")
    return value


def _optional_string(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise StoreError("scheduler value is invalid")
    return value


def _int(data: dict[str, object], key: str) -> int:
    value = data[key]
    if type(value) is not int:
        raise StoreError("scheduler value is invalid")
    return value


def _optional_int(value: object) -> int | None:
    if value is not None and type(value) is not int:
        raise StoreError("scheduler value is invalid")
    return value


def _bool(data: dict[str, object], key: str) -> bool:
    value = data[key]
    if type(value) is not bool:
        raise StoreError("scheduler value is invalid")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StoreError("scheduler list is invalid")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise StoreError("scheduler list is invalid")
    return value


def _ints(value: object) -> list[int]:
    if not isinstance(value, list) or not all(type(item) is int for item in value):
        raise StoreError("scheduler list is invalid")
    return value


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise StoreError("scheduler timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StoreError("scheduler timestamp is invalid") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise StoreError("scheduler timestamp is not UTC")
    return timestamp


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _time_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _id(value: str) -> None:
    if _ID.fullmatch(value) is None:
        raise StoreError("scheduler identifier is invalid")


def _write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except OSError as exc:
        Path(handle.name).unlink(missing_ok=True)
        raise StoreError("scheduler write failed") from exc


def _read(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StoreError("scheduler document is invalid") from exc


def _encode_cursor(value: int) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).decode()


def _cursor(value: str | None) -> int:
    if value is None:
        return 0
    try:
        result = int(base64.urlsafe_b64decode(value.encode()).decode())
    except (ValueError, UnicodeError) as exc:
        raise StoreError("scheduler cursor is invalid") from exc
    if result < 0:
        raise StoreError("scheduler cursor is invalid")
    return result
