"""Focused JSON TaskStore persistence coverage."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.infrastructure.persistence import scheduler as scheduler_persistence
from sjtuclaw.infrastructure.persistence.scheduler import JsonTaskStore
from sjtuclaw.models.scheduler import (
    Occurrence,
    OccurrenceStatus,
    Schedule,
    ScheduleKind,
    Task,
    TaskArchiveReason,
    TaskStatus,
)


def test_task_store_round_trips_tasks_occurrences_and_cursor(tmp_path: Path) -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    task = Task(
        "task_001",
        "session_001",
        "整理资料",
        Schedule(ScheduleKind.INTERVAL, now, interval_seconds=300),
        TaskStatus.ACTIVE,
        now + timedelta(minutes=5),
        now,
        now,
    )
    store = JsonTaskStore(tmp_path)
    store.create_task(task)
    first = Occurrence(
        "occurrence_001", task.task_id, 1, OccurrenceStatus.PLANNED, now, now, now
    )
    second = Occurrence(
        "occurrence_002", task.task_id, 2, OccurrenceStatus.PLANNED, now, now, now
    )
    store.create_occurrence(first)
    store.create_occurrence(second)

    assert store.load_task(task.task_id) == task
    page, cursor = store.list_occurrences(task.task_id, limit=1)
    assert page == (first,)
    assert cursor is not None
    page, cursor = store.list_occurrences(task.task_id, cursor=cursor, limit=1)
    assert page == (second,)
    assert cursor is None


def test_task_delete_is_retry_safe_when_occurrence_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    task = Task(
        "task_001",
        "session_001",
        "cleanup",
        Schedule(ScheduleKind.ONCE, now),
        TaskStatus.ACTIVE,
        now,
        now,
        now,
    )
    archived = replace(
        task,
        status=TaskStatus.CANCELLED,
        next_run_at=None,
        archived_at=now,
        archived_reason=TaskArchiveReason.USER_CANCELLED,
    )
    store = JsonTaskStore(tmp_path)
    store.create_task(archived)
    store.create_occurrence(
        Occurrence(
            "occurrence_001",
            task.task_id,
            1,
            OccurrenceStatus.CANCELLED_BEFORE_START,
            now,
            now,
            now,
        )
    )
    original_rmtree = scheduler_persistence.shutil.rmtree

    def fail_once(_path: Path) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(scheduler_persistence.shutil, "rmtree", fail_once)
    with pytest.raises(StoreError, match="failed to delete task"):
        store.delete_task(task.task_id)

    assert store.load_task(task.task_id) == archived
    monkeypatch.setattr(scheduler_persistence.shutil, "rmtree", original_rmtree)
    store.delete_task(task.task_id)
    store.delete_task(task.task_id)
    assert not (tmp_path / "scheduler" / "occurrences" / task.task_id).exists()
