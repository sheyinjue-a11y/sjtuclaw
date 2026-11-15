"""Focused deterministic SchedulerService coverage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.infrastructure.persistence.scheduler import JsonTaskStore
from sjtuclaw.models.runtime import RuntimeRequest
from sjtuclaw.models.scheduler import (
    OccurrenceStatus,
    Schedule,
    ScheduleKind,
    TaskStatus,
)
from sjtuclaw.models.session import RunState, RunStatus, Session, SessionArchiveReason
from sjtuclaw.models.skill import SkillReference
from sjtuclaw.runtime.scheduler import SchedulerService
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from tests.fakes import FakeClock, FakeIdGenerator


class FakeRuntime:
    def __init__(self) -> None:
        self.requests: list[RuntimeRequest] = []
        self.runs: dict[str, RunState] = {}
        self.fail_next_submit = False
        self.fail_cancel = False
        self.fail_stop = False
        self.race_queued_to_started = False
        self.cancel_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.submitted = asyncio.Event()

    async def submit(self, request: RuntimeRequest) -> object:
        if self.fail_next_submit:
            self.fail_next_submit = False
            raise RuntimeError("runtime unavailable")
        self.requests.append(request)
        self.submitted.set()
        self.runs[request.idempotency_key or request.request_id] = RunState(
            run_id=f"run_{len(self.requests):03d}",
            request_id=request.request_id,
            status=RunStatus.QUEUED,
            idempotency_key=request.idempotency_key,
        )
        return object()

    def find_run(
        self,
        _session_id: str,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunState | None:
        if idempotency_key is not None:
            return self.runs.get(idempotency_key)
        return next((item for item in self.runs.values() if item.request_id == request_id), None)

    async def cancel_queued(self, _session_id: str, request_id: str) -> bool:
        self.cancel_calls.append(request_id)
        for key, run in self.runs.items():
            if run.request_id == request_id and run.status is RunStatus.QUEUED:
                if self.race_queued_to_started:
                    self.race_queued_to_started = False
                    self.runs[key] = replace(
                        run,
                        status=RunStatus.STARTED,
                        started_at=datetime(2026, 7, 20, tzinfo=UTC),
                    )
                    return False
                if self.fail_cancel:
                    return False
                self.runs[key] = replace(run, status=RunStatus.CANCELLED_BEFORE_START)
                return True
        return False

    async def stop_run(self, _session_id: str, run_id: str) -> bool:
        self.stop_calls.append(run_id)
        for key, run in self.runs.items():
            if run.run_id == run_id and run.status is RunStatus.STARTED:
                if self.fail_stop:
                    return False
                self.runs[key] = replace(run, status=RunStatus.CANCELLED)
                return True
        return False

    def complete(self, idempotency_key: str, now: datetime) -> None:
        run = self.runs[idempotency_key]
        self.runs[idempotency_key] = replace(
            run,
            status=RunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
            duration_ms=0,
            assistant_message_id="message_001",
        )


def _service(
    tmp_path: Path, clock: FakeClock
) -> tuple[SchedulerService, JsonTaskStore, FakeRuntime]:
    store = JsonTaskStore(tmp_path)
    runtime = FakeRuntime()
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "scheduler", (), "", clock.current, clock.current))
    return (
        SchedulerService(
            tasks=store,
            runtime=runtime,
            sessions=sessions,
            coordinator=SessionCoordinator(),
            clock=clock,
            ids=FakeIdGenerator(),
        ),
        store,
        runtime,
    )


def test_interval_recovery_coalesces_and_skips_overlap(tmp_path: Path) -> None:
    async def scenario() -> None:
        anchor = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(anchor)
        service, store, runtime = _service(tmp_path, clock)
        task = await service.create(
            session_id="session_001",
            instruction="check status",
            schedule=Schedule(ScheduleKind.INTERVAL, anchor, interval_seconds=300),
        )

        clock.current = anchor + timedelta(minutes=12)
        await service.recover()

        first, cursor = store.list_occurrences(task.task_id)
        assert cursor is None
        assert len(first) == 1
        assert first[0].status is OccurrenceStatus.QUEUED
        assert first[0].coalesced is True
        assert first[0].missed_count == 2
        assert runtime.requests[0].source.value == "scheduler"
        assert store.load_task(task.task_id).next_run_at == anchor + timedelta(minutes=15)

        clock.current = anchor + timedelta(minutes=15)
        await service.tick()

        history, _ = store.list_occurrences(task.task_id)
        assert [item.status for item in history] == [
            OccurrenceStatus.QUEUED,
            OccurrenceStatus.SKIPPED_OVERLAP,
        ]
        assert len(runtime.requests) == 1

    asyncio.run(scenario())


def test_planned_occurrence_is_retried_with_its_stable_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(now)
        service, store, runtime = _service(tmp_path, clock)
        task = await service.create(
            session_id="session_001",
            instruction="run once",
            schedule=Schedule(ScheduleKind.ONCE, now + timedelta(minutes=1)),
        )
        clock.current += timedelta(minutes=1)
        runtime.fail_next_submit = True

        await service.tick()
        planned, _ = store.list_occurrences(task.task_id)
        assert planned[0].status is OccurrenceStatus.PLANNED

        await service.recover()
        recovered, _ = store.list_occurrences(task.task_id)
        assert recovered[0].status is OccurrenceStatus.QUEUED
        assert runtime.requests[0].idempotency_key == f"occurrence:{recovered[0].occurrence_id}"
        assert len(runtime.requests) == 1

    asyncio.run(scenario())


def test_cancel_cancels_queue_but_preserves_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(now)
        service, store, _runtime = _service(tmp_path, clock)
        task = await service.create(
            session_id="session_001",
            instruction="run once",
            schedule=Schedule(ScheduleKind.ONCE, now + timedelta(minutes=1)),
        )
        clock.current += timedelta(minutes=1)
        await service.tick()

        cancelled = await service.cancel(task.task_id)
        history, _ = store.list_occurrences(task.task_id)
        assert cancelled.status is TaskStatus.CANCELLED
        assert cancelled.next_run_at is None
        assert history[0].status is OccurrenceStatus.CANCELLED_BEFORE_START
        assert await service.cancel(task.task_id) == cancelled

    asyncio.run(scenario())


def test_once_task_becomes_completed_after_runtime_terminal_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(now)
        service, store, runtime = _service(tmp_path, clock)
        task = await service.create(
            session_id="session_001",
            instruction="run once",
            schedule=Schedule(ScheduleKind.ONCE, now + timedelta(minutes=1)),
        )
        clock.current += timedelta(minutes=1)
        await service.tick()
        occurrence, _ = store.list_occurrences(task.task_id)
        runtime.complete(f"occurrence:{occurrence[0].occurrence_id}", clock.current)

        await service.tick()

        assert store.load_task(task.task_id).status is TaskStatus.COMPLETED
        history, _ = store.list_occurrences(task.task_id)
        assert history[0].status is OccurrenceStatus.COMPLETED

    asyncio.run(scenario())


def test_background_loop_wakes_when_an_earlier_task_is_created(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(now)
        service, _store, runtime = _service(tmp_path, clock)
        await service.create(
            session_id="session_001",
            instruction="run tomorrow",
            schedule=Schedule(ScheduleKind.ONCE, now + timedelta(days=1)),
        )
        service.start()
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await service.create(
                session_id="session_001",
                instruction="run now",
                schedule=Schedule(ScheduleKind.ONCE, now),
            )

            await asyncio.wait_for(runtime.submitted.wait(), timeout=0.5)

            assert [request.user_input.text for request in runtime.requests] == ["run now"]
        finally:
            await service.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid_case",
    ["past_schedule", "empty_instruction", "invalid_permission", "duplicate_skill"],
)
def test_invalid_update_preserves_old_task_occurrence_and_run(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(now)
        service, store, runtime = _service(tmp_path, clock)
        task = await service.create(
            session_id="session_001",
            instruction="old instruction",
            schedule=Schedule(ScheduleKind.ONCE, now + timedelta(minutes=1)),
        )
        clock.current += timedelta(minutes=1)
        await service.tick()
        task = store.load_task(task.task_id)
        occurrence = store.list_occurrences(task.task_id)[0][0]
        run = runtime.runs[f"occurrence:{occurrence.occurrence_id}"]
        instruction = "new instruction"
        schedule = Schedule(ScheduleKind.ONCE, now + timedelta(hours=1))
        permissions: tuple[str, ...] = ()
        skills: tuple[SkillReference, ...] = ()
        if invalid_case == "past_schedule":
            schedule = Schedule(ScheduleKind.ONCE, now)
        elif invalid_case == "empty_instruction":
            instruction = "  "
        elif invalid_case == "invalid_permission":
            permissions = ("unknown",)
        else:
            skills = (
                SkillReference("same", f"same-{'a' * 16}", "a" * 64, "1"),
                SkillReference("same", f"same-{'b' * 16}", "b" * 64, "2"),
            )

        with pytest.raises((ValueError, RuntimeError, AttributeError)):
            await service.update(
                task.task_id,
                session_id=task.session_id,
                instruction=instruction,
                schedule=schedule,
                workspace_binding_revision=None,
                managed_permissions=permissions,
                skill_refs=skills,
            )

        assert store.load_task(task.task_id) == task
        assert store.load_occurrence(task.task_id, occurrence.occurrence_id) == occurrence
        assert runtime.runs[f"occurrence:{occurrence.occurrence_id}"] == run

    asyncio.run(scenario())


def test_update_stop_failure_preserves_old_task_and_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(now)
        service, store, runtime = _service(tmp_path, clock)
        task = await service.create(
            session_id="session_001",
            instruction="old instruction",
            schedule=Schedule(ScheduleKind.ONCE, now + timedelta(minutes=1)),
        )
        clock.current += timedelta(minutes=1)
        await service.tick()
        task = store.load_task(task.task_id)
        occurrence = store.list_occurrences(task.task_id)[0][0]
        run = runtime.runs[f"occurrence:{occurrence.occurrence_id}"]
        runtime.fail_cancel = True

        with pytest.raises(RuntimeError, match="terminal"):
            await service.update(
                task.task_id,
                session_id=task.session_id,
                instruction="new instruction",
                schedule=Schedule(ScheduleKind.ONCE, now + timedelta(hours=1)),
                workspace_binding_revision=None,
                managed_permissions=(),
                skill_refs=(),
            )

        assert store.load_task(task.task_id) == task
        assert store.load_occurrence(task.task_id, occurrence.occurrence_id) == occurrence
        assert runtime.runs[f"occurrence:{occurrence.occurrence_id}"] == run

    asyncio.run(scenario())


def test_archive_stops_run_that_wins_queued_to_started_race(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(now)
        service, store, runtime = _service(tmp_path, clock)
        task = await service.create(
            session_id="session_001",
            instruction="run once",
            schedule=Schedule(ScheduleKind.ONCE, now + timedelta(minutes=1)),
        )
        clock.current += timedelta(minutes=1)
        await service.tick()
        occurrence = store.list_occurrences(task.task_id)[0][0]
        runtime.race_queued_to_started = True

        archived = await service.cancel(task.task_id)
        stopped = store.load_occurrence(task.task_id, occurrence.occurrence_id)

        assert archived.status is TaskStatus.CANCELLED
        assert runtime.stop_calls == ["run_001"]
        assert stopped.status is OccurrenceStatus.FAILED
        assert stopped.error_code == "CANCELLED_BY_TASK_ARCHIVE"
        assert runtime.runs[f"occurrence:{occurrence.occurrence_id}"].status is (
            RunStatus.CANCELLED
        )

    asyncio.run(scenario())


def test_archived_task_retry_is_rejected_without_new_occurrence(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        clock = FakeClock(now)
        service, store, _runtime = _service(tmp_path, clock)
        task = await service.create(
            session_id="session_001",
            instruction="run once",
            schedule=Schedule(ScheduleKind.ONCE, now + timedelta(minutes=1)),
        )
        clock.current += timedelta(minutes=1)
        await service.tick()
        queued = store.list_occurrences(task.task_id)[0][0]
        failed = replace(
            queued,
            status=OccurrenceStatus.FAILED,
            error_code="LLM_ERROR",
        )
        store.save_occurrence(failed)
        await service.cancel(task.task_id)
        before = store.list_occurrences(task.task_id)[0]

        with pytest.raises(ValueError, match="archived"):
            await service.retry(task.task_id, failed.occurrence_id)

        assert store.list_occurrences(task.task_id)[0] == before

    asyncio.run(scenario())


def test_scheduler_rejects_create_for_archived_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, tzinfo=UTC)
        sessions = InMemorySessionStore()
        sessions.create(
            Session(
                "session_001",
                "archived",
                (),
                "",
                now,
                now,
                archived_at=now,
                archived_reason=SessionArchiveReason.USER_ARCHIVED,
            )
        )
        service = SchedulerService(
            tasks=JsonTaskStore(tmp_path),
            runtime=FakeRuntime(),
            sessions=sessions,
            coordinator=SessionCoordinator(),
            clock=FakeClock(now),
            ids=FakeIdGenerator(),
        )

        with pytest.raises(Exception, match="SESSION_ARCHIVED"):
            await service.create(
                session_id="session_001",
                instruction="must not schedule",
                schedule=Schedule(ScheduleKind.ONCE, now + timedelta(minutes=1)),
            )

        assert service.list() == ()

    asyncio.run(scenario())
