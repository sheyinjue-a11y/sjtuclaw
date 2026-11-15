"""Durable, clock-driven Scheduler orchestration over the shared RuntimeHost."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

from sjtuclaw.models.runtime import InvocationSource, RuntimeRequest, UserInput
from sjtuclaw.models.scheduler import (
    Occurrence,
    OccurrenceStatus,
    Schedule,
    ScheduleKind,
    Task,
    TaskArchiveReason,
    TaskStatus,
    next_run_after,
)
from sjtuclaw.models.session import RunState, RunStatus
from sjtuclaw.models.skill import SkillReference
from sjtuclaw.ports.runtime import Clock, IdGenerator
from sjtuclaw.ports.scheduler import SchedulerRuntime, TaskStore
from sjtuclaw.ports.stores import SessionStore
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_state import require_session_mutable


class SchedulerService:
    """Persisted task scheduler that never bypasses RuntimeHost admission.

    Callers drive :meth:`tick` from their process lifecycle.  The service does
    not own a sleep loop, which keeps time deterministic and makes restart
    recovery an ordinary tick over durable state.
    """

    def __init__(
        self,
        *,
        tasks: TaskStore,
        runtime: SchedulerRuntime,
        sessions: SessionStore,
        coordinator: SessionCoordinator,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._tasks = tasks
        self._runtime = runtime
        self._sessions = sessions
        self._coordinator = coordinator
        self._clock = clock
        self._ids = ids
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._background_task: asyncio.Task[None] | None = None

    async def create(
        self,
        *,
        session_id: str,
        instruction: str,
        schedule: Schedule,
        workspace_binding_revision: int | None = None,
        managed_permissions: tuple[str, ...] = (),
        skill_refs: tuple[SkillReference, ...] = (),
    ) -> Task:
        """Create a task whose next deadline is calculated from its anchor."""

        async with self._lock:
            now = self._clock.now()
            next_run_at = _next_at_or_after(schedule, now)
            if next_run_at is None:
                raise ValueError("schedule has no future occurrence")
            task = Task(
                task_id=self._ids.new_id("task"),
                session_id=session_id,
                instruction=instruction.strip(),
                schedule=schedule,
                status=TaskStatus.ACTIVE,
                next_run_at=next_run_at,
                created_at=now,
                updated_at=now,
                workspace_binding_revision=workspace_binding_revision,
                managed_permissions=managed_permissions,
                skill_refs=skill_refs,
            )
            async with self._coordinator.admission_lock(session_id):
                require_session_mutable(self._sessions.load(session_id))
                self._tasks.create_task(task)
            self._wakeup_event.set()
            return task

    async def tick(self) -> None:
        """Reconcile durable runs, then form at most one due occurrence/task."""

        async with self._lock:
            for task in self._tasks.list_tasks():
                try:
                    await self._require_session_mutable(task.session_id)
                    await self._reconcile_task(task)
                except Exception:
                    # One damaged task or an unavailable Session/Runtime must
                    # not prevent other independent tasks from being examined.
                    continue
            for task in self._tasks.list_tasks():
                try:
                    if task.status is TaskStatus.CANCEL_REQUESTED:
                        await self._finish_cancellation(task)
                    elif task.status is TaskStatus.ACTIVE:
                        await self._require_session_mutable(task.session_id)
                        await self._trigger_due(task)
                except Exception:
                    continue

    async def recover(self) -> None:
        """Resume only persisted planned work, then coalesce missed deadlines."""

        await self.tick()

    def start(self) -> None:
        """Start the production wakeup loop after the application is recovered."""

        if self._background_task is None:
            self._stop_event.clear()
            self._wakeup_event.clear()
            self._background_task = asyncio.create_task(self._serve(), name="scheduler")

    async def aclose(self) -> None:
        """Stop the wakeup loop without changing durable task state."""

        self._stop_event.set()
        self._wakeup_event.set()
        if self._background_task is not None:
            await self._background_task
            self._background_task = None

    async def cancel(self, task_id: str) -> Task:
        """Archive a task after stopping its current scheduled work."""

        return await self.archive(task_id, TaskArchiveReason.USER_CANCELLED)

    async def archive(self, task_id: str, reason: TaskArchiveReason) -> Task:
        """Stop a task and retain it in the archive with its history."""

        async with self._lock:
            task = self._tasks.load_task(task_id)
            if task.archived_at is not None:
                return task
            if reason is not TaskArchiveReason.SESSION_ARCHIVED:
                async with self._coordinator.admission_lock(task.session_id):
                    require_session_mutable(self._sessions.load(task.session_id))
            requested = replace(
                task,
                status=TaskStatus.CANCEL_REQUESTED,
                next_run_at=None,
                updated_at=self._clock.now(),
            )
            self._tasks.save_task(requested)
            self._wakeup_event.set()
            await self._stop_task_occurrences(requested, "CANCELLED_BY_TASK_ARCHIVE")
            archived = replace(
                self._tasks.load_task(task_id),
                status=TaskStatus.CANCELLED,
                next_run_at=None,
                archived_at=self._clock.now(),
                archived_reason=reason,
                updated_at=self._clock.now(),
            )
            self._tasks.save_task(archived)
            return archived

    async def update(
        self,
        task_id: str,
        *,
        session_id: str,
        instruction: str,
        schedule: Schedule,
        workspace_binding_revision: int | None,
        managed_permissions: tuple[str, ...],
        skill_refs: tuple[SkillReference, ...],
    ) -> Task:
        """Replace future task configuration after stopping current work."""

        async with self._lock:
            current = self._tasks.load_task(task_id)
            if current.archived_at is not None:
                raise ValueError("archived task must be restored before editing")
            if session_id != current.session_id:
                raise ValueError("task session cannot be changed")
            next_run_at = _next_at_or_after(schedule, self._clock.now())
            if next_run_at is None:
                raise ValueError("schedule has no future occurrence")
            updated = Task(
                task_id=current.task_id,
                session_id=session_id,
                instruction=instruction.strip(),
                schedule=schedule,
                status=TaskStatus.ACTIVE,
                next_run_at=next_run_at,
                created_at=current.created_at,
                updated_at=self._clock.now(),
                workspace_binding_revision=workspace_binding_revision,
                managed_permissions=managed_permissions,
                skill_refs=skill_refs,
            )
            async with self._coordinator.admission_lock(current.session_id):
                require_session_mutable(self._sessions.load(current.session_id))
            await self._stop_task_occurrences(current, "CANCELLED_BY_TASK_EDIT")
            async with self._coordinator.admission_lock(current.session_id):
                require_session_mutable(self._sessions.load(current.session_id))
                self._tasks.save_task(updated)
            self._wakeup_event.set()
            return updated

    async def restore(self, task_id: str) -> Task:
        async with self._lock:
            task = self._tasks.load_task(task_id)
            if task.archived_at is None:
                return task
            restored = replace(
                task,
                archived_at=None,
                archived_reason=None,
                updated_at=self._clock.now(),
            )
            async with self._coordinator.admission_lock(task.session_id):
                require_session_mutable(self._sessions.load(task.session_id))
                self._tasks.save_task(restored)
            return restored

    def purge(self, task_id: str) -> None:
        self._tasks.delete_task(task_id)

    async def archive_for_session(self, session_id: str) -> tuple[Task, ...]:
        values = tuple(task for task in self._tasks.list_tasks() if task.session_id == session_id)
        archived: list[Task] = []
        for task in values:
            archived.append(
                await self.archive(task.task_id, TaskArchiveReason.SESSION_ARCHIVED)
            )
        return tuple(archived)

    async def purge_for_session(self, session_id: str) -> None:
        await self.archive_for_session(session_id)
        for task in tuple(
            task
            for task in self._tasks.list_tasks(archived=True)
            if task.session_id == session_id
        ):
            self.purge(task.task_id)

    async def retry(self, task_id: str, occurrence_id: str) -> Occurrence:
        """Manually retry one failed occurrence once through RuntimeHost."""

        async with self._lock:
            task = self._tasks.load_task(task_id)
            if task.archived_at is not None:
                raise ValueError("archived task occurrences cannot be retried")
            previous = self._tasks.load_occurrence(task_id, occurrence_id)
            if previous.status is not OccurrenceStatus.FAILED:
                raise ValueError("only failed occurrences can be retried")
            occurrence = self._new_occurrence(
                task,
                scheduled_for=self._clock.now(),
                retry_of_occurrence_id=previous.occurrence_id,
            )
            async with self._coordinator.admission_lock(task.session_id):
                require_session_mutable(self._sessions.load(task.session_id))
                self._tasks.create_occurrence(occurrence)
            return await self._submit_planned(task, occurrence)

    def list(self, *, archived: bool = False) -> tuple[Task, ...]:
        return self._tasks.list_tasks(archived=archived)

    def detail(self, task_id: str) -> Task:
        return self._tasks.load_task(task_id)

    def history(
        self, task_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[tuple[Occurrence, ...], str | None]:
        return self._tasks.list_occurrences(task_id, cursor=cursor, limit=limit)

    async def _reconcile_task(self, task: Task) -> None:
        completed: Occurrence | None = None
        failed: Occurrence | None = None
        for occurrence in self._all_occurrences(task.task_id):
            if occurrence.status is OccurrenceStatus.COMPLETED:
                completed = occurrence
                continue
            if occurrence.status is OccurrenceStatus.FAILED:
                failed = occurrence
                continue
            if occurrence.status not in {
                OccurrenceStatus.PLANNED,
                OccurrenceStatus.QUEUED,
                OccurrenceStatus.RUNNING,
            }:
                continue
            run = self._runtime.find_run(
                task.session_id,
                idempotency_key=_occurrence_key(occurrence.occurrence_id),
            )
            if run is None:
                if occurrence.status is OccurrenceStatus.PLANNED:
                    await self._submit_planned(task, occurrence)
                continue
            reconciled = _with_run(occurrence, run, self._clock.now())
            if reconciled != occurrence:
                self._tasks.save_occurrence(reconciled)
            if reconciled.status is OccurrenceStatus.COMPLETED:
                completed = reconciled
            elif reconciled.status is OccurrenceStatus.FAILED:
                failed = reconciled
        if task.schedule.kind is ScheduleKind.ONCE and task.status is TaskStatus.ACTIVE:
            terminal = completed or failed
            if terminal is not None:
                self._tasks.save_task(
                    replace(
                        task,
                        status=(
                            TaskStatus.COMPLETED
                            if terminal.status is OccurrenceStatus.COMPLETED
                            else TaskStatus.FAILED
                        ),
                        next_run_at=None,
                        updated_at=self._clock.now(),
                    )
                )

    async def _trigger_due(self, task: Task) -> None:
        if task.next_run_at is None or task.next_run_at > self._clock.now():
            return
        existing = self._occurrence_for_deadline(task.task_id, task.next_run_at)
        if existing is not None:
            self._advance_task(task)
            return

        now = self._clock.now()
        scheduled_for = task.next_run_at
        missed_count = _missed_count(task.schedule, scheduled_for, now)
        active = next((item for item in self._all_occurrences(task.task_id) if item.is_active), None)
        if active is not None:
            occurrence = self._new_occurrence(
                task,
                scheduled_for=scheduled_for,
                missed_count=missed_count,
                blocking_occurrence_id=active.occurrence_id,
                status=OccurrenceStatus.SKIPPED_OVERLAP,
            )
            self._tasks.create_occurrence(occurrence)
            self._advance_task(task)
            return

        occurrence = self._new_occurrence(
            task,
            scheduled_for=scheduled_for,
            missed_count=missed_count,
        )
        # This write is deliberately before RuntimeHost.submit.  If the process
        # stops here recovery finds this planned occurrence and submits exactly
        # the same occurrence idempotency key.
        self._tasks.create_occurrence(occurrence)
        self._advance_task(task)
        await self._submit_planned(task, occurrence)

    async def _submit_planned(self, task: Task, occurrence: Occurrence) -> Occurrence:
        if occurrence.status is not OccurrenceStatus.PLANNED:
            return occurrence
        request = RuntimeRequest(
            request_id=self._ids.new_id("request"),
            session_id=task.session_id,
            source=InvocationSource.SCHEDULER,
            user_input=UserInput(task.instruction),
            actor_id=f"scheduler:{task.task_id}",
            idempotency_key=_occurrence_key(occurrence.occurrence_id),
            scheduler_task_id=task.task_id,
            occurrence_id=occurrence.occurrence_id,
            workspace_binding_revision=task.workspace_binding_revision,
            managed_permission_categories=task.managed_permissions,
            skill_refs=task.skill_refs,
        )
        try:
            await self._runtime.submit(request)
        except Exception:
            # The occurrence remains PLANNED.  Its durable idempotency key is
            # retried on the next tick/recovery instead of inventing a new Run.
            return occurrence
        run = self._runtime.find_run(
            task.session_id,
            idempotency_key=_occurrence_key(occurrence.occurrence_id),
        )
        if run is None:
            raise RuntimeError("runtime accepted occurrence without a durable run")
        queued = _with_run(occurrence, run, self._clock.now())
        self._tasks.save_occurrence(queued)
        return queued

    async def _finish_cancellation(self, task: Task) -> None:
        await self._stop_task_occurrences(task, "CANCELLED_BY_TASK_ARCHIVE")
        current = self._tasks.load_task(task.task_id)
        if current.status is TaskStatus.CANCEL_REQUESTED:
            self._tasks.save_task(
                replace(
                    current,
                    status=TaskStatus.CANCELLED,
                    next_run_at=None,
                    updated_at=self._clock.now(),
                )
            )

    async def _stop_task_occurrences(self, task: Task, error_code: str) -> None:
        occurrences = sorted(
            self._all_occurrences(task.task_id),
            key=lambda item: item.status is OccurrenceStatus.PLANNED,
        )
        for occurrence in occurrences:
            await self._stop_occurrence(task, occurrence, error_code)

    async def _stop_occurrence(
        self,
        task: Task,
        occurrence: Occurrence,
        error_code: str,
    ) -> None:
        if occurrence.status is OccurrenceStatus.PLANNED:
            self._tasks.save_occurrence(
                replace(
                    occurrence,
                    status=OccurrenceStatus.CANCELLED_BEFORE_START,
                    updated_at=self._clock.now(),
                )
            )
            return
        if occurrence.status not in {
            OccurrenceStatus.QUEUED,
            OccurrenceStatus.RUNNING,
        }:
            return

        run = self._runtime.find_run(
            task.session_id,
            idempotency_key=_occurrence_key(occurrence.occurrence_id),
        )
        if occurrence.status is OccurrenceStatus.QUEUED:
            assert occurrence.request_id is not None
            if await self._runtime.cancel_queued(task.session_id, occurrence.request_id):
                self._tasks.save_occurrence(
                    replace(
                        occurrence,
                        status=OccurrenceStatus.CANCELLED_BEFORE_START,
                        updated_at=self._clock.now(),
                    )
                )
                return
            run = self._runtime.find_run(
                task.session_id,
                idempotency_key=_occurrence_key(occurrence.occurrence_id),
            )
            if run is None:
                self._tasks.save_occurrence(
                    replace(
                        occurrence,
                        status=OccurrenceStatus.CANCELLED_BEFORE_START,
                        updated_at=self._clock.now(),
                    )
                )
                return

        if run is None:
            raise RuntimeError("scheduled occurrence has no durable terminal run")
        if run.status is RunStatus.STARTED:
            await self._runtime.stop_run(task.session_id, run.run_id)
            run = self._runtime.find_run(
                task.session_id,
                idempotency_key=_occurrence_key(occurrence.occurrence_id),
            )
        if run is None or run.status in {RunStatus.QUEUED, RunStatus.STARTED}:
            raise RuntimeError("scheduled occurrence did not reach a terminal run")
        if run.status is RunStatus.CANCELLED_BEFORE_START:
            stopped = replace(
                occurrence,
                status=OccurrenceStatus.CANCELLED_BEFORE_START,
                updated_at=self._clock.now(),
            )
        elif run.status is RunStatus.COMPLETED and run.assistant_message_id is not None:
            stopped = _with_run(occurrence, run, self._clock.now())
        else:
            stopped = replace(
                occurrence,
                status=OccurrenceStatus.FAILED,
                queued_at=occurrence.queued_at or self._clock.now(),
                request_id=occurrence.request_id or run.request_id,
                run_id=occurrence.run_id or run.run_id,
                error_code=error_code,
                updated_at=self._clock.now(),
            )
        self._tasks.save_occurrence(stopped)

    def _advance_task(self, task: Task) -> None:
        if task.schedule.kind is ScheduleKind.ONCE:
            # Keep the original due time while its sole occurrence is active.
            # _reconcile_task turns it into COMPLETED or FAILED once RuntimeHost
            # has a durable terminal result.
            updated = replace(task, updated_at=self._clock.now())
        else:
            updated = replace(
                task,
                next_run_at=next_run_after(task.schedule, self._clock.now()),
                updated_at=self._clock.now(),
            )
        self._tasks.save_task(updated)

    async def _serve(self) -> None:
        while not self._stop_event.is_set():
            # Clear before reconciliation so a concurrent create/cancel during
            # tick remains visible and forces one immediate recalculation.
            self._wakeup_event.clear()
            await self.tick()
            if self._stop_event.is_set():
                return
            delay = self._next_wakeup_delay_seconds()
            try:
                await asyncio.wait_for(self._wakeup_event.wait(), timeout=delay)
            except TimeoutError:
                continue

    def _next_wakeup_delay_seconds(self) -> float:
        now = self._clock.now()
        try:
            deadlines = [
                task.next_run_at
                for task in self._tasks.list_tasks()
                if task.status is TaskStatus.ACTIVE and task.next_run_at is not None
            ]
        except Exception:
            return 60.0
        if not deadlines:
            return 60.0
        return max(0.0, min((deadline - now).total_seconds() for deadline in deadlines))

    def _new_occurrence(
        self,
        task: Task,
        *,
        scheduled_for: datetime,
        status: OccurrenceStatus = OccurrenceStatus.PLANNED,
        missed_count: int = 0,
        retry_of_occurrence_id: str | None = None,
        blocking_occurrence_id: str | None = None,
    ) -> Occurrence:
        now = self._clock.now()
        return Occurrence(
            occurrence_id=self._ids.new_id("occurrence"),
            task_id=task.task_id,
            sequence_number=len(self._all_occurrences(task.task_id)) + 1,
            status=status,
            scheduled_for=scheduled_for,
            created_at=now,
            updated_at=now,
            coalesced=missed_count > 0,
            missed_count=missed_count,
            retry_of_occurrence_id=retry_of_occurrence_id,
            blocking_occurrence_id=blocking_occurrence_id,
        )

    def _all_occurrences(self, task_id: str) -> tuple[Occurrence, ...]:
        values: list[Occurrence] = []
        cursor: str | None = None
        while True:
            page, cursor = self._tasks.list_occurrences(task_id, cursor=cursor, limit=100)
            values.extend(page)
            if cursor is None:
                return tuple(values)

    async def _require_session_mutable(self, session_id: str) -> None:
        async with self._coordinator.admission_lock(session_id):
            require_session_mutable(self._sessions.load(session_id))

    def _occurrence_for_deadline(
        self, task_id: str, deadline: datetime
    ) -> Occurrence | None:
        return next(
            (
                occurrence
                for occurrence in self._all_occurrences(task_id)
                if occurrence.scheduled_for == deadline
            ),
            None,
        )


def _next_at_or_after(schedule: Schedule, now: datetime) -> datetime | None:
    return next_run_after(schedule, now - timedelta(microseconds=1))


def _missed_count(schedule: Schedule, scheduled_for: datetime, now: datetime) -> int:
    missed = 0
    cursor = scheduled_for
    while True:
        next_deadline = next_run_after(schedule, cursor)
        if next_deadline is None or next_deadline > now:
            return missed
        missed += 1
        cursor = next_deadline


def _with_run(occurrence: Occurrence, run: RunState, now: datetime) -> Occurrence:
    status = run.status
    if status is RunStatus.QUEUED:
        return replace(
            occurrence,
            status=OccurrenceStatus.QUEUED,
            queued_at=now,
            request_id=run.request_id,
            updated_at=now,
        )
    if status is RunStatus.STARTED:
        return replace(
            occurrence,
            status=OccurrenceStatus.RUNNING,
            queued_at=occurrence.queued_at or now,
            request_id=run.request_id,
            run_id=run.run_id,
            updated_at=now,
        )
    if status is RunStatus.COMPLETED and run.assistant_message_id is not None:
        return replace(
            occurrence,
            status=OccurrenceStatus.COMPLETED,
            queued_at=occurrence.queued_at or now,
            request_id=run.request_id,
            run_id=run.run_id,
            assistant_message_id=run.assistant_message_id,
            updated_at=now,
        )
    return replace(
        occurrence,
        status=OccurrenceStatus.FAILED,
        queued_at=occurrence.queued_at or now,
        request_id=run.request_id,
        run_id=run.run_id,
        error_code=(
            "RUN_COMPLETED_NO_MESSAGE"
            if status is RunStatus.COMPLETED
            else status.value.upper()
        ),
        updated_at=now,
    )


def _occurrence_key(occurrence_id: str) -> str:
    return f"occurrence:{occurrence_id}"
