"""Unified RuntimeHost submit interface and queued Agent Runtime."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import Final

from sjtuclaw.errors import ApprovalRequired, SJTUClawError
from sjtuclaw.models.attachment import Attachment
from sjtuclaw.models.llm import LLMToolMode, LLMUsage
from sjtuclaw.models.message import MessageRole
from sjtuclaw.models.run_record import RunRecord, RunRecordType
from sjtuclaw.models.runtime import (
    InvocationSource,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRequest,
    UserInput,
)
from sjtuclaw.models.session import (
    ApprovalDecision,
    ApprovalDecisionStatus,
    PendingApproval,
    PendingRequest,
    RunState,
    RunStatus,
    Session,
)
from sjtuclaw.ports.attachments import AttachmentStore
from sjtuclaw.ports.llm import LLMClient
from sjtuclaw.ports.runtime import Clock, IdGenerator
from sjtuclaw.ports.stores import SessionStore
from sjtuclaw.ports.tools import ToolExecutor
from sjtuclaw.runtime.agent_loop import AgentLoop
from sjtuclaw.runtime.compaction import CompactionService
from sjtuclaw.runtime.context_builder import ContextBuilder
from sjtuclaw.runtime.memory_capture import MemoryCapture
from sjtuclaw.runtime.permission import PermissionPolicy
from sjtuclaw.runtime.provider_runtime import ProviderRuntime
from sjtuclaw.runtime.run_control import RunControl, SteerCommand
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_state import require_session_mutable
from sjtuclaw.runtime.session_title import (
    SessionTitleGenerator,
    SessionTitleResult,
    fallback_session_title,
)
from sjtuclaw.runtime.skill_routing import (
    candidate_default_skill_ids,
    merge_default_skill_refs,
)
from sjtuclaw.runtime.skill_service import SkillService
from sjtuclaw.runtime.token_usage import accumulate_token_usage

_TERMINAL_EVENTS: Final = {
    RuntimeEventType.RUN_COMPLETED,
    RuntimeEventType.RUN_FAILED,
    RuntimeEventType.RUN_CANCELLED,
}
_DEFAULT_SESSION_TITLE: Final = "新会话"


def _combine_llm_usage(left: LLMUsage, right: LLMUsage) -> LLMUsage:
    cache_available = all(
        value is not None
        for value in (
            left.prompt_cache_hit_tokens,
            left.prompt_cache_miss_tokens,
            right.prompt_cache_hit_tokens,
            right.prompt_cache_miss_tokens,
        )
    )
    return LLMUsage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        prompt_cache_hit_tokens=(
            (left.prompt_cache_hit_tokens or 0)
            + (right.prompt_cache_hit_tokens or 0)
            if cache_available
            else None
        ),
        prompt_cache_miss_tokens=(
            (left.prompt_cache_miss_tokens or 0)
            + (right.prompt_cache_miss_tokens or 0)
            if cache_available
            else None
        ),
    )


class RunHandle:
    def __init__(self, cancelled_event: RuntimeEvent) -> None:
        self._events: asyncio.Queue[RuntimeEvent] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._cancelled_event = cancelled_event
        self._terminal_published = False
        self._cancellation_requested = False

    def attach(self, task: asyncio.Task[None]) -> None:
        if self._terminal_published:
            raise RuntimeError("terminal run handle cannot accept a task")
        if self._task is not None and not self._task.done():
            raise RuntimeError("run handle already has a task")
        self._task = task
        self._cancellation_requested = False
        task.add_done_callback(self._on_task_done)

    def publish(self, event: RuntimeEvent) -> None:
        if self._terminal_published:
            return
        self._events.put_nowait(event)
        if event.event_type in _TERMINAL_EVENTS:
            self._terminal_published = True

    @property
    def is_terminal(self) -> bool:
        return self._terminal_published

    @property
    def task_is_done(self) -> bool:
        return self._task is None or self._task.done()

    async def events(self) -> AsyncIterator[RuntimeEvent]:
        while True:
            event = await self._events.get()
            yield event
            if event.event_type in _TERMINAL_EVENTS:
                return

    def cancel(self) -> bool:
        if (
            self._cancellation_requested
            or self._task is None
            or self._task.done()
        ):
            return False
        self._cancellation_requested = True
        self._task.cancel()
        return True

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self.publish(self._cancelled_event)


class AgentRuntime:
    """Keep per-session admission and locking outside the reusable agent loop."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        sessions: SessionStore,
        context_builder: ContextBuilder,
        coordinator: SessionCoordinator,
        clock: Clock,
        ids: IdGenerator,
        compaction: CompactionService | None = None,
        tools: ToolExecutor | None = None,
        permission: PermissionPolicy | None = None,
        tool_mode: LLMToolMode | None = None,
        max_steps: int = 20,
        repeat_limit: int = 5,
        skills: SkillService | None = None,
        memory_capture: MemoryCapture | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._sessions = sessions
        self._clock = clock
        self._loop = AgentLoop(
            llm=llm,
            sessions=sessions,
            context_builder=context_builder,
            coordinator=coordinator,
            clock=clock,
            ids=ids,
            compaction=compaction,
            tools=tools,
            permission=permission,
            tool_mode=tool_mode,
            max_steps=max_steps,
            repeat_limit=repeat_limit,
            skills=skills,
            memory_capture=memory_capture,
        )
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(self, request: RuntimeRequest) -> RunHandle:
        invocation_sequence = await self._coordinator.reserve_invocation_sequence(
            request.session_id
        )
        cancelled_event = self._event(
            RuntimeEventType.RUN_CANCELLED,
            request,
            invocation_sequence,
        )
        handle = RunHandle(cancelled_event)
        handle.publish(
            self._event(RuntimeEventType.REQUEST_ACCEPTED, request, invocation_sequence)
        )
        handle.publish(
            self._event(RuntimeEventType.REQUEST_QUEUED, request, invocation_sequence)
        )
        task = asyncio.create_task(self._run(request, invocation_sequence, handle))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        handle.attach(task)
        return handle

    async def aclose(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        request: RuntimeRequest,
        invocation_sequence: int,
        handle: RunHandle,
    ) -> None:
        try:
            async with self._coordinator.execution_lock(request.session_id):
                handle.publish(
                    self._event(RuntimeEventType.RUN_STARTED, request, invocation_sequence)
                )
                usage = await self._loop.run_locked(
                    request,
                    lambda event_type, payload: handle.publish(
                        self._event(
                            event_type,
                            request,
                            invocation_sequence,
                            payload,
                        )
                    ),
                )
                handle.publish(
                    self._event(
                        RuntimeEventType.RUN_COMPLETED,
                        request,
                        invocation_sequence,
                        {"usage": _usage_payload(usage)},
                    )
                )
        except ApprovalRequired:
            # AgentLoop already persisted the checkpoint and emitted the
            # approval event.  Releasing this task frees a global permit while
            # keeping the Session ordered behind the approval.
            return
        except asyncio.CancelledError:
            handle.publish(
                self._event(RuntimeEventType.RUN_CANCELLED, request, invocation_sequence)
            )
        except SJTUClawError as exc:
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_FAILED,
                    request,
                    invocation_sequence,
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )
            )
        except Exception:
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_FAILED,
                    request,
                    invocation_sequence,
                    {
                        "error_type": "UnexpectedRuntimeError",
                        "message": "unexpected runtime failure",
                    },
                )
            )

    @staticmethod
    def _event(
        event_type: RuntimeEventType,
        request: RuntimeRequest,
        sequence_number: int,
        payload: dict[str, object] | None = None,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            event_type=event_type,
            session_id=request.session_id,
            invocation_id=request.request_id,
            sequence_number=sequence_number,
            source=request.source,
            payload=_freeze_payload(payload),
        )


def _usage_payload(usage: LLMUsage) -> dict[str, int]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def _freeze_payload(payload: dict[str, object] | None) -> Mapping[str, object]:
    values = {} if payload is None else payload
    return MappingProxyType({key: _freeze_value(value) for key, value in values.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _normalize_approval_reason(
    status: ApprovalDecisionStatus,
    reason: str | None,
) -> str | None:
    if status is not ApprovalDecisionStatus.DENIED:
        if reason is not None:
            raise ValueError("only denied approvals may include a reason")
        return None
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise ValueError("approval denial reason must be text")
    if any(ord(character) < 32 or ord(character) == 127 for character in reason):
        raise ValueError("approval denial reason must not contain control characters")
    normalized = reason.strip()
    if not normalized:
        return None
    if len(normalized) > 1_000:
        raise ValueError("approval denial reason must be at most 1000 characters")
    return normalized


class RuntimeHost(AgentRuntime):
    """Persistent fair admission queue for CLI, Gateway, and Scheduler."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        sessions: SessionStore,
        context_builder: ContextBuilder,
        coordinator: SessionCoordinator,
        clock: Clock,
        ids: IdGenerator,
        compaction: CompactionService | None = None,
        tools: ToolExecutor | None = None,
        permission: PermissionPolicy | None = None,
        tool_mode: LLMToolMode | None = None,
        max_steps: int = 20,
        repeat_limit: int = 5,
        max_concurrent_runs: int = 2,
        attachments: AttachmentStore | None = None,
        skills: SkillService | None = None,
        session_titles: SessionTitleGenerator | None = None,
        provider_runtime: ProviderRuntime | None = None,
        memory_capture: MemoryCapture | None = None,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be positive")
        super().__init__(
            llm=llm,
            sessions=sessions,
            context_builder=context_builder,
            coordinator=coordinator,
            clock=clock,
            ids=ids,
            compaction=compaction,
            tools=tools,
            permission=permission,
            tool_mode=tool_mode,
            max_steps=max_steps,
            repeat_limit=repeat_limit,
            skills=skills,
            memory_capture=memory_capture,
        )
        self._ids = ids
        self._attachments = attachments
        self._skills = skills
        self._session_titles = session_titles
        self._provider_runtime = provider_runtime
        self._max_concurrent_runs = max_concurrent_runs
        self._ready_sessions: deque[str] = deque()
        self._ready_session_ids: set[str] = set()
        self._active_sessions: set[str] = set()
        self._handles: dict[str, RunHandle] = {}
        self._controls: dict[str, RunControl] = {}
        self._active_requests: dict[str, RuntimeRequest] = {}
        self._invocation_sequences: dict[str, int] = {}
        self._scheduled_tasks: set[asyncio.Task[None]] = set()
        self._submitted_monotonic_ms: dict[str, int] = {}
        self._recovered_submitted_at: dict[str, datetime] = {}
        self._resuming_approval_ids: set[str] = set()
        self._dispatch_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._approval_timeout_task: asyncio.Task[None] | None = None
        self._closed = False

    async def submit(self, request: RuntimeRequest) -> RunHandle:
        if self._closed:
            raise RuntimeError("runtime host is closed")
        self._ensure_approval_timeout_scanner()
        request = self._route_default_skills(request)
        run, accepted = await self._persist_pending(request)
        if not accepted:
            existing = self._handles.get(run.request_id)
            if existing is not None:
                return (
                    self._terminal_handle(replace(request, request_id=run.request_id), run)
                    if existing.is_terminal
                    else existing
                )
            if run.status is RunStatus.QUEUED:
                session = self._sessions.load(request.session_id)
                pending = next(
                    item
                    for item in session.pending_requests
                    if item.request_id == run.request_id
                )
                await self._resume_pending(request.session_id, pending)
                return self._handles[run.request_id]
            return self._terminal_handle(replace(request, request_id=run.request_id), run)
        sequence = await self._coordinator.reserve_invocation_sequence(request.session_id)
        handle = RunHandle(
            self._event(RuntimeEventType.RUN_CANCELLED, request, sequence)
        )
        handle.publish(self._event(RuntimeEventType.REQUEST_ACCEPTED, request, sequence))
        handle.publish(self._event(RuntimeEventType.REQUEST_QUEUED, request, sequence))
        self._handles[request.request_id] = handle
        self._invocation_sequences[request.request_id] = sequence
        self._submitted_monotonic_ms[request.request_id] = self._clock.monotonic_ms()
        await self._enqueue_session(request.session_id)
        return handle

    def _route_default_skills(self, request: RuntimeRequest) -> RuntimeRequest:
        if self._skills is None:
            return request
        attachments: tuple[Attachment, ...] = ()
        if request.attachment_refs and self._attachments is not None:
            attachments = self._attachments.require_ready(
                request.session_id,
                request.attachment_refs,
            )
        return merge_default_skill_refs(
            request,
            candidate_default_skill_ids(request.user_input.text, attachments),
            self._skills,
        )

    async def expire_approvals(self) -> int:
        """Persist due timeout decisions; the normal fair queue performs resume."""

        expired = 0
        now = self._clock.now()
        for session in self._sessions.list():
            approval = session.pending_approval
            if approval is None or approval.expires_at > now:
                continue
            try:
                await self.decide_approval(
                    session.session_id,
                    approval.approval_id,
                    status=ApprovalDecisionStatus.TIMED_OUT,
                    idempotency_key=f"system-timeout:{approval.approval_id}",
                    actor_id="system_timeout",
                )
            except ValueError:
                # A user decision or stop won the durable race.  Its state is
                # authoritative, so this scanner must not overwrite it.
                continue
            expired += 1
        return expired

    def _ensure_approval_timeout_scanner(self) -> None:
        if self._approval_timeout_task is None or self._approval_timeout_task.done():
            self._approval_timeout_task = asyncio.create_task(
                self._approval_timeout_loop(),
                name="runtime-approval-timeouts",
            )

    async def _approval_timeout_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(1)
            try:
                await self.expire_approvals()
            except Exception:
                # Individual decisions remain recoverable through the next tick
                # or RuntimeHost recovery; a scanner error must not kill polling.
                continue

    async def cancel_queued(self, session_id: str, request_id: str) -> bool:
        """Cancel only queued work; a started run is controlled by its RunHandle."""

        async with self._coordinator.admission_lock(session_id):
            session = self._sessions.load(session_id)
            pending = next(
                (
                    item
                    for item in session.pending_requests
                    if item.request_id == request_id
                ),
                None,
            )
            if pending is None:
                return False
            runs = tuple(
                RunState(
                    run_id=run.run_id,
                    request_id=run.request_id,
                    status=(
                        RunStatus.CANCELLED_BEFORE_START
                        if run.request_id == request_id
                        else run.status
                    ),
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                    duration_ms=run.duration_ms,
                    assistant_message_id=run.assistant_message_id,
                    idempotency_key=run.idempotency_key,
                    checkpoint=run.checkpoint,
                    selected_skill_refs=run.selected_skill_refs,
                )
                for run in session.runs
            )
            self._sessions.save(
                replace(
                    session,
                    pending_requests=tuple(
                        item
                        for item in session.pending_requests
                        if item.request_id != request_id
                    ),
                    runs=runs,
                    updated_at=self._clock.now(),
                )
            )
        handle = self._handles.get(request_id)
        if handle is not None:
            handle.publish(
                RuntimeEvent(
                    event_type=RuntimeEventType.RUN_CANCELLED,
                    session_id=session_id,
                    invocation_id=request_id,
                    sequence_number=self._invocation_sequences.get(request_id, 0),
                    source=pending.source,
                    payload=MappingProxyType({"code": "CANCELLED_BEFORE_START"}),
                )
            )
        return True

    async def stop_run(self, session_id: str, run_id: str) -> bool:
        """Stop one active run without affecting later queued requests."""

        handle_to_wait: RunHandle | None = None
        async with self._coordinator.admission_lock(session_id):
            session = self._sessions.load(session_id)
            run = next((item for item in session.runs if item.run_id == run_id), None)
            if run is None or run.status is not RunStatus.STARTED:
                return False
            handle = self._handles.get(run.request_id)
            pending = session.pending_approval
            if (
                pending is not None
                and pending.run_id == run_id
                and (handle is None or handle.task_is_done)
            ):
                request = self._request_from_checkpoint(session_id, run)
                now = self._clock.now()
                self._sessions.save(
                    replace(
                        session,
                        pending_approval=None,
                        runs=tuple(
                            replace(
                                item,
                                status=RunStatus.CANCELLED,
                                completed_at=now,
                                duration_ms=self._duration_ms(item.request_id, now),
                                checkpoint=None,
                            )
                            if item.run_id == run_id
                            else item
                            for item in session.runs
                        ),
                        updated_at=now,
                    )
                )
                if handle is not None:
                    handle.publish(
                        self._event(
                            RuntimeEventType.RUN_CANCELLED,
                            request,
                            self._invocation_sequences.get(run.request_id, 0),
                            {"run_id": run_id, "code": "CANCELLED"},
                        )
                    )
                return True
            if handle is None or not handle.cancel():
                return False
            handle_to_wait = handle
        await handle_to_wait.wait()
        terminal = next(
            (
                item
                for item in self._sessions.load(session_id).runs
                if item.run_id == run_id
            ),
            None,
        )
        return terminal is not None and terminal.status not in {
            RunStatus.QUEUED,
            RunStatus.STARTED,
        }

    async def stop_session(self, session_id: str) -> None:
        """Cancel every queued or active request before session archival."""

        session = self._sessions.load(session_id)
        for pending in tuple(session.pending_requests):
            await self.cancel_queued(session_id, pending.request_id)
        await self._cancel_orphaned_queued_runs(session_id)
        session = self._sessions.load(session_id)
        for run in tuple(session.runs):
            if run.status is RunStatus.STARTED:
                await self.stop_run(session_id, run.run_id)
        remaining = self._sessions.load(session_id)
        if remaining.pending_requests or any(
            run.status in {RunStatus.QUEUED, RunStatus.STARTED}
            for run in remaining.runs
        ):
            raise RuntimeError("session still has active runtime work")

    async def _cancel_orphaned_queued_runs(self, session_id: str) -> None:
        """Terminally cancel durable queued Runs that have lost their request."""

        async with self._coordinator.admission_lock(session_id):
            session = self._sessions.load(session_id)
            pending_request_ids = {
                request.request_id for request in session.pending_requests
            }
            orphaned_run_ids = {
                run.run_id
                for run in session.runs
                if run.status is RunStatus.QUEUED
                and run.request_id not in pending_request_ids
            }
            if not orphaned_run_ids:
                return
            self._sessions.save(
                replace(
                    session,
                    runs=tuple(
                        replace(run, status=RunStatus.CANCELLED_BEFORE_START)
                        if run.run_id in orphaned_run_ids
                        else run
                        for run in session.runs
                    ),
                    updated_at=self._clock.now(),
                )
            )

    async def steer(
        self,
        session_id: str,
        run_id: str,
        *,
        steer_id: str,
        idempotency_key: str,
        text: str,
    ) -> str:
        """Apply input to one active Run at AgentLoop's next safe point."""

        async with self._coordinator.admission_lock(session_id):
            session = self._sessions.load(session_id)
            require_session_mutable(session)
            run = next((item for item in session.runs if item.run_id == run_id), None)
            control = self._controls.get(run_id)
            request = self._active_requests.get(run_id)
            if run is None or run.status is not RunStatus.STARTED or control is None or request is None:
                return "RUN_NOT_ACTIVE"
            result = await control.request(
                SteerCommand(
                    steer_id=steer_id,
                    idempotency_key=idempotency_key,
                    text=text,
                )
            )
            handle = self._handles.get(run.request_id)
            if handle is not None:
                sequence = self._invocation_sequences.get(run.request_id, 0)
                if result == "STEER_ACCEPTED":
                    handle.publish(
                        self._event(
                            RuntimeEventType.STEER_ACCEPTED,
                            request,
                            sequence,
                            {"steer_id": steer_id},
                        )
                    )
                    handle.publish(
                        self._event(
                            RuntimeEventType.STEER_WAITING_SAFE_POINT,
                            request,
                            sequence,
                            {"steer_id": steer_id},
                        )
                    )
                elif result != "STEER_ACCEPTED":
                    handle.publish(
                        self._event(
                            RuntimeEventType.STEER_REJECTED,
                            request,
                            sequence,
                            {"steer_id": steer_id, "code": result},
                        )
                    )
            return result

    async def steer_queued(
        self,
        session_id: str,
        run_id: str,
        *,
        request_id: str,
        steer_id: str,
        idempotency_key: str,
    ) -> str:
        """Convert one still-queued request into guidance exactly once."""

        async with self._coordinator.admission_lock(session_id):
            session = self._sessions.load(session_id)
            require_session_mutable(session)
            run = next((item for item in session.runs if item.run_id == run_id), None)
            control = self._controls.get(run_id)
            active_request = self._active_requests.get(run_id)
            if (
                run is None
                or run.status is not RunStatus.STARTED
                or control is None
                or active_request is None
            ):
                return "RUN_NOT_ACTIVE"
            previous = await control.accepted_result(steer_id, idempotency_key)
            if previous is not None:
                return previous
            pending = next(
                (
                    item
                    for item in session.pending_requests
                    if item.request_id == request_id
                ),
                None,
            )
            if pending is None:
                return "QUEUED_REQUEST_NOT_FOUND"
            if pending.attachment_refs or pending.skill_refs:
                return "QUEUED_REQUEST_NOT_GUIDABLE"
            command = SteerCommand(
                steer_id=steer_id,
                idempotency_key=idempotency_key,
                text=pending.text,
            )

            def commit() -> None:
                now = self._clock.now()
                self._sessions.save(
                    replace(
                        session,
                        pending_requests=tuple(
                            item
                            for item in session.pending_requests
                            if item.request_id != request_id
                        ),
                        runs=tuple(
                            replace(
                                item,
                                status=RunStatus.CANCELLED_BEFORE_START,
                            )
                            if item.request_id == request_id
                            and item.status is RunStatus.QUEUED
                            else item
                            for item in session.runs
                        ),
                        updated_at=now,
                    )
                )

            result = await control.request_after(command, commit)
            if result != "STEER_ACCEPTED":
                return result
            queued_handle = self._handles.get(request_id)
            if queued_handle is not None:
                queued_handle.publish(
                    RuntimeEvent(
                        event_type=RuntimeEventType.RUN_CANCELLED,
                        session_id=session_id,
                        invocation_id=request_id,
                        sequence_number=self._invocation_sequences.get(request_id, 0),
                        source=pending.source,
                        payload=MappingProxyType({"code": "CANCELLED_BEFORE_START"}),
                    )
                )
            active_handle = self._handles.get(run.request_id)
            if active_handle is not None:
                sequence = self._invocation_sequences.get(run.request_id, 0)
                active_handle.publish(
                    self._event(
                        RuntimeEventType.STEER_ACCEPTED,
                        active_request,
                        sequence,
                        {"steer_id": steer_id},
                    )
                )
                active_handle.publish(
                    self._event(
                        RuntimeEventType.STEER_WAITING_SAFE_POINT,
                        active_request,
                        sequence,
                        {"steer_id": steer_id},
                    )
                )
            return result

    async def decide_approval(
        self,
        session_id: str,
        approval_id: str,
        *,
        status: ApprovalDecisionStatus,
        idempotency_key: str,
        actor_id: str,
        reason: str | None = None,
    ) -> ApprovalDecision:
        """Persist an idempotent decision, then resume the paused Run once."""

        normalized_reason = _normalize_approval_reason(status, reason)
        async with self._coordinator.admission_lock(session_id):
            session = self._sessions.load(session_id)
            require_session_mutable(session)
            existing = next(
                (
                    item
                    for item in session.approval_decisions
                    if item.idempotency_key == idempotency_key
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.approval_id != approval_id
                    or existing.status is not status
                    or existing.actor_id != actor_id
                    or existing.reason != normalized_reason
                ):
                    raise ValueError("approval idempotency key conflicts")
                decision = existing
                approval = session.pending_approval
                if approval is None or approval.approval_id != approval_id:
                    return decision
            else:
                prior_decision = next(
                    (
                        item
                        for item in session.approval_decisions
                        if item.approval_id == approval_id
                    ),
                    None,
                )
                if prior_decision is not None:
                    raise ValueError("approval was already decided")
                approval = session.pending_approval
                if approval is None or approval.approval_id != approval_id:
                    raise ValueError("approval is not pending")
                decision = ApprovalDecision(
                    decision_id=self._ids.new_id("approval_decision"),
                    approval_id=approval_id,
                    status=status,
                    idempotency_key=idempotency_key,
                    actor_id=actor_id,
                    decided_at=self._clock.now(),
                    reason=normalized_reason,
                )
                self._sessions.save(
                    replace(
                        session,
                        approval_decisions=(*session.approval_decisions, decision),
                        updated_at=self._clock.now(),
                    )
                )
        if approval is not None:
            try:
                await self._schedule_approval_resume(session_id, approval, decision)
            except Exception:
                await self._rollback_approval_decision(session_id, decision)
                raise
        return decision

    async def _rollback_approval_decision(
        self,
        session_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Remove a decision only when its queue handoff could not be established."""

        async with self._coordinator.admission_lock(session_id):
            session = self._sessions.load(session_id)
            if decision not in session.approval_decisions:
                return
            self._sessions.save(
                replace(
                    session,
                    approval_decisions=tuple(
                        item
                        for item in session.approval_decisions
                        if item != decision
                    ),
                    updated_at=self._clock.now(),
                )
            )

    def find_run(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunState | None:
        """Return one durable Run without exposing the Session store to entrypoints."""

        if request_id is None and idempotency_key is None:
            raise ValueError("request_id or idempotency_key is required")
        session = self._sessions.load(session_id)
        return next(
            (
                run
                for run in session.runs
                if (request_id is not None and run.request_id == request_id)
                or (
                    idempotency_key is not None
                    and run.idempotency_key == idempotency_key
                )
            ),
            None,
        )

    def handle_for_run(self, session_id: str, run_id: str) -> RunHandle | None:
        """Return the in-memory event source only after durable Session ownership checks."""

        run = next(
            (item for item in self._sessions.load(session_id).runs if item.run_id == run_id),
            None,
        )
        return None if run is None else self._handles.get(run.request_id)

    async def recover_pending(self) -> None:
        """Mark in-flight runs interrupted, then resume durable queued requests."""

        self._ensure_approval_timeout_scanner()
        await self.expire_approvals()
        async with self._recovery_lock:
            for listed in self._sessions.list():
                resumes: list[tuple[PendingApproval, ApprovalDecision]] = []
                async with self._coordinator.admission_lock(listed.session_id):
                    session = self._sessions.load(listed.session_id)
                    pending = session.pending_approval
                    records = [*session.run_records]
                    next_sequence = (
                        1 if not records else records[-1].sequence_number + 1
                    )
                    runs: list[RunState] = []
                    for run in session.runs:
                        if run.status is not RunStatus.STARTED:
                            runs.append(run)
                            continue
                        checkpoint = run.checkpoint
                        if checkpoint is None:
                            runs.append(replace(run, status=RunStatus.INTERRUPTED))
                            records.append(
                                self._recovery_record(
                                    run.request_id,
                                    "RUN_INTERRUPTED_GATEWAY_RESTART",
                                    "run interrupted by runtime host restart",
                                    next_sequence,
                                )
                            )
                            next_sequence += 1
                            continue
                        decision = next(
                            (
                                item
                                for item in session.approval_decisions
                                if item.approval_id == checkpoint.pending_approval_id
                            ),
                            None,
                        )
                        if pending is not None and pending.run_id == run.run_id:
                            if (
                                decision is not None
                                and decision.status is ApprovalDecisionStatus.APPROVED
                                and checkpoint.approved_execution_started
                            ):
                                runs.append(
                                    replace(
                                        run,
                                        status=RunStatus.INTERRUPTED,
                                        checkpoint=None,
                                    )
                                )
                                pending = None
                                records.append(
                                    self._recovery_record(
                                        run.request_id,
                                        "APPROVED_TOOL_EXECUTION_INTERRUPTED",
                                        "approved tool execution was interrupted before its result was saved",
                                        next_sequence,
                                    )
                                )
                                next_sequence += 1
                            else:
                                runs.append(run)
                                if decision is not None:
                                    resumes.append((pending, decision))
                            continue

                        # A previous build cleared PendingApproval before its
                        # result commit.  There is no safe way to reconstruct an
                        # approved side effect, so preserve the audit trail and
                        # terminate rather than replaying it.
                        runs.append(
                            replace(
                                run,
                                status=RunStatus.INTERRUPTED,
                                checkpoint=None,
                            )
                        )
                        records.append(
                            self._recovery_record(
                                run.request_id,
                                (
                                    "APPROVED_TOOL_EXECUTION_INTERRUPTED"
                                    if decision is not None
                                    and decision.status is ApprovalDecisionStatus.APPROVED
                                    else "APPROVAL_RECOVERY_STATE_MISSING"
                                ),
                                "approval recovery state was incomplete after runtime restart",
                                next_sequence,
                            )
                        )
                        next_sequence += 1

                    updated = replace(
                        session,
                        runs=tuple(runs),
                        pending_approval=pending,
                        run_records=tuple(records),
                        updated_at=self._clock.now(),
                    )
                    if updated != session:
                        self._sessions.save(updated)
                    pending_requests = updated.pending_requests
                for request in pending_requests:
                    await self._resume_pending(listed.session_id, request)
                for approval, decision in resumes:
                    await self._schedule_approval_resume(
                        listed.session_id,
                        approval,
                        decision,
                    )

    def _recovery_record(
        self,
        request_id: str,
        code: str,
        message: str,
        sequence_number: int,
    ) -> RunRecord:
        return RunRecord(
            record_id=self._ids.new_id("record"),
            invocation_id=request_id,
            record_type=RunRecordType.STATUS,
            code=code,
            message=message,
            created_at=self._clock.now(),
            sequence_number=sequence_number,
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._approval_timeout_task is not None:
            self._approval_timeout_task.cancel()
        for task in tuple(self._scheduled_tasks):
            task.cancel()
        tasks = tuple(self._scheduled_tasks)
        if self._approval_timeout_task is not None:
            tasks = (*tasks, self._approval_timeout_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().aclose()

    async def _persist_pending(self, request: RuntimeRequest) -> tuple[RunState, bool]:
        async with self._coordinator.admission_lock(request.session_id):
            session = self._sessions.load(request.session_id)
            require_session_mutable(session)
            if self._attachments is not None:
                self._attachments.require_ready(
                    request.session_id,
                    request.attachment_refs,
                )
            existing_request = next(
                (run for run in session.runs if run.request_id == request.request_id),
                None,
            )
            if existing_request is not None:
                return existing_request, False
            if request.idempotency_key is not None:
                existing = next(
                    (
                        run
                        for run in session.runs
                        if run.idempotency_key == request.idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    return existing, False
            sequence = max(
                (item.acceptance_sequence for item in session.pending_requests),
                default=0,
            ) + 1
            now = self._clock.now()
            effective_skill_refs = request.skill_refs
            if not effective_skill_refs:
                # Selected Skills are session state: a follow-up request must
                # inherit the latest run's references instead of silently
                # dropping them and breaking read_skill_resource.
                inherited = next(
                    (
                        run.selected_skill_refs
                        for run in reversed(session.runs)
                        if run.selected_skill_refs
                    ),
                    None,
                )
                if inherited is not None:
                    effective_skill_refs = inherited
            pending = PendingRequest(
                request_id=request.request_id,
                acceptance_sequence=sequence,
                source=request.source,
                actor_id=request.actor_id,
                text=request.user_input.text,
                submitted_at=now,
                attachment_refs=request.attachment_refs,
                idempotency_key=request.idempotency_key,
                scheduler_task_id=request.scheduler_task_id,
                occurrence_id=request.occurrence_id,
                workspace_binding_revision=request.workspace_binding_revision,
                managed_permission_categories=request.managed_permission_categories,
                skill_refs=effective_skill_refs,
                automatic_skill_ids=request.automatic_skill_ids,
            )
            run = RunState(
                run_id=self._ids.new_id("run"),
                request_id=request.request_id,
                status=RunStatus.QUEUED,
                idempotency_key=request.idempotency_key,
                selected_skill_refs=effective_skill_refs,
            )
            self._sessions.save(
                replace(
                    session,
                    pending_requests=(*session.pending_requests, pending),
                    runs=(*session.runs, run),
                    updated_at=now,
                )
            )
            return run, True

    async def _resume_pending(self, session_id: str, pending: PendingRequest) -> None:
        if pending.request_id in self._handles:
            return
        self._recovered_submitted_at[pending.request_id] = pending.submitted_at
        request = RuntimeRequest(
            request_id=pending.request_id,
            session_id=session_id,
            source=pending.source,
            user_input=UserInput(pending.text),
            actor_id=pending.actor_id,
            attachment_refs=pending.attachment_refs,
            idempotency_key=pending.idempotency_key,
            scheduler_task_id=pending.scheduler_task_id,
            occurrence_id=pending.occurrence_id,
            workspace_binding_revision=pending.workspace_binding_revision,
            managed_permission_categories=pending.managed_permission_categories,
            skill_refs=pending.skill_refs,
            automatic_skill_ids=pending.automatic_skill_ids,
        )
        sequence = await self._coordinator.reserve_invocation_sequence(session_id)
        handle = RunHandle(self._event(RuntimeEventType.RUN_CANCELLED, request, sequence))
        self._handles[request.request_id] = handle
        self._invocation_sequences[request.request_id] = sequence
        self._submitted_monotonic_ms[request.request_id] = self._clock.monotonic_ms()
        await self._enqueue_session(session_id)

    async def _schedule_approval_resume(
        self,
        session_id: str,
        approval: PendingApproval,
        decision: ApprovalDecision,
    ) -> None:
        """Put one decided approval back into the same fair admission queue."""

        session = self._sessions.load(session_id)
        if session.pending_approval != approval:
            return
        run = next((item for item in session.runs if item.run_id == approval.run_id), None)
        if run is None or run.checkpoint is None:
            raise ValueError("approval run checkpoint is unavailable")
        if run.checkpoint.approved_execution_started and (
            decision.status is ApprovalDecisionStatus.APPROVED
        ):
            raise ValueError("approved tool execution already started")
        await self._enqueue_session(session_id)

    @staticmethod
    def _request_from_checkpoint(session_id: str, run: RunState) -> RuntimeRequest:
        if run.checkpoint is None:
            raise ValueError("approval run checkpoint is unavailable")
        checkpoint = run.checkpoint
        return RuntimeRequest(
            request_id=checkpoint.request_id,
            session_id=session_id,
            source=checkpoint.source,
            user_input=UserInput(checkpoint.input_text),
            actor_id=checkpoint.actor_id,
            attachment_refs=checkpoint.attachment_refs,
            idempotency_key=run.idempotency_key,
            skill_refs=checkpoint.skill_refs,
            automatic_skill_ids=checkpoint.automatic_skill_ids,
        )

    async def _resume_approval(
        self,
        request: RuntimeRequest,
        approval: PendingApproval,
        decision: ApprovalDecision,
        handle: RunHandle,
    ) -> None:
        if self._provider_runtime is not None:
            async with self._provider_runtime.lease():
                await self._resume_approval_leased(request, approval, decision, handle)
            return
        await self._resume_approval_leased(request, approval, decision, handle)

    async def _resume_approval_leased(
        self,
        request: RuntimeRequest,
        approval: PendingApproval,
        decision: ApprovalDecision,
        handle: RunHandle,
    ) -> None:
        run_id = approval.run_id
        sequence = self._invocation_sequences[request.request_id]
        try:
            async with self._coordinator.execution_lock(request.session_id):
                async with self._coordinator.admission_lock(request.session_id):
                    session = self._sessions.load(request.session_id)
                    if session.pending_approval != approval:
                        return
                    self._controls[run_id] = RunControl()
                    self._active_requests[run_id] = request
                handle.publish(
                    self._event(
                        RuntimeEventType.APPROVAL_RESUMED,
                        request,
                        sequence,
                        {"approval_id": approval.approval_id, "status": decision.status.value},
                    )
                )
                usage = await self._loop.resume_approval_locked(
                    request,
                    approval,
                    decision,
                    lambda event_type, payload: handle.publish(
                        self._event(event_type, request, sequence, payload)
                    ),
                    on_completed=lambda session, assistant_id: self._complete_run(
                        session, request, assistant_id
                    ),
                    control=self._controls[run_id],
                )
                title_usage = await self._apply_automatic_session_title(request)
                if title_usage is not None:
                    usage = _combine_llm_usage(usage, title_usage)
                completed = next(
                    item
                    for item in self._sessions.load(request.session_id).runs
                    if item.run_id == run_id
                )
                handle.publish(
                    self._event(
                        RuntimeEventType.RUN_COMPLETED,
                        request,
                        sequence,
                        {
                            "usage": _usage_payload(usage),
                            "run_id": run_id,
                            "duration_ms": completed.duration_ms,
                            "assistant_message_id": completed.assistant_message_id,
                        },
                    )
                )
        except ApprovalRequired:
            return
        except asyncio.CancelledError:
            await self._mark_terminal(request, RunStatus.CANCELLED)
            handle.publish(self._event(RuntimeEventType.RUN_CANCELLED, request, sequence))
        except SJTUClawError as exc:
            await self._mark_terminal(request, RunStatus.FAILED)
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_FAILED,
                    request,
                    sequence,
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )
            )
        except Exception:
            await self._mark_terminal(request, RunStatus.FAILED)
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_FAILED,
                    request,
                    sequence,
                    {
                        "error_type": "UnexpectedRuntimeError",
                        "message": "unexpected runtime failure",
                    },
                )
            )
        finally:
            self._resuming_approval_ids.discard(approval.approval_id)
            self._controls.pop(run_id, None)
            self._active_requests.pop(run_id, None)
            async with self._dispatch_lock:
                self._active_sessions.discard(request.session_id)
                self._queue_session_if_runnable_locked(
                    self._sessions.load(request.session_id)
                )
            await self._dispatch()

    async def _enqueue_session(self, session_id: str) -> None:
        async with self._dispatch_lock:
            self._queue_session_if_runnable_locked(self._sessions.load(session_id))
        await self._dispatch()

    def _queue_session_if_runnable_locked(self, session: Session) -> None:
        session_id = session.session_id
        if (
            session_id in self._active_sessions
            or session_id in self._ready_session_ids
            or session.archived_at is not None
            or not self._session_is_runnable(session)
        ):
            return
        self._ready_sessions.append(session_id)
        self._ready_session_ids.add(session_id)

    @staticmethod
    def _session_is_runnable(session: Session) -> bool:
        approval = session.pending_approval
        if approval is None:
            return bool(session.pending_requests)
        decision = next(
            (
                item
                for item in session.approval_decisions
                if item.approval_id == approval.approval_id
            ),
            None,
        )
        run = next((item for item in session.runs if item.run_id == approval.run_id), None)
        return (
            decision is not None
            and run is not None
            and run.checkpoint is not None
            and not (
                decision.status is ApprovalDecisionStatus.APPROVED
                and run.checkpoint.approved_execution_started
            )
        )

    async def _dispatch(self) -> None:
        async with self._dispatch_lock:
            while (
                not self._closed
                and len(self._active_sessions) < self._max_concurrent_runs
                and self._ready_sessions
            ):
                session_id = self._ready_sessions.popleft()
                self._ready_session_ids.remove(session_id)
                if session_id in self._active_sessions:
                    continue
                session = self._sessions.load(session_id)
                if session.archived_at is not None:
                    continue
                approval = session.pending_approval
                if approval is not None:
                    decision = next(
                        (
                            item
                            for item in session.approval_decisions
                            if item.approval_id == approval.approval_id
                        ),
                        None,
                    )
                    run = next(
                        (item for item in session.runs if item.run_id == approval.run_id),
                        None,
                    )
                    if (
                        decision is None
                        or run is None
                        or run.checkpoint is None
                        or (
                            decision.status is ApprovalDecisionStatus.APPROVED
                            and run.checkpoint.approved_execution_started
                        )
                    ):
                        continue
                    request = self._request_from_checkpoint(session_id, run)
                    sequence = self._invocation_sequences.get(request.request_id)
                    if sequence is None:
                        sequence = await self._coordinator.reserve_invocation_sequence(session_id)
                        self._invocation_sequences[request.request_id] = sequence
                    handle = self._handles.get(request.request_id)
                    if handle is None or handle.is_terminal:
                        handle = RunHandle(
                            self._event(
                                RuntimeEventType.RUN_CANCELLED,
                                request,
                                sequence,
                            )
                        )
                        self._handles[request.request_id] = handle
                    scheduled_operation = self._resume_approval(
                        request,
                        approval,
                        decision,
                        handle,
                    )
                    task_name = f"approval-{approval.approval_id}"
                    self._resuming_approval_ids.add(approval.approval_id)
                else:
                    if not session.pending_requests:
                        continue
                    pending = min(
                        session.pending_requests,
                        key=lambda item: item.acceptance_sequence,
                    )
                    request = RuntimeRequest(
                        request_id=pending.request_id,
                        session_id=session_id,
                        source=pending.source,
                        user_input=UserInput(pending.text),
                        actor_id=pending.actor_id,
                        attachment_refs=pending.attachment_refs,
                        idempotency_key=pending.idempotency_key,
                        scheduler_task_id=pending.scheduler_task_id,
                        occurrence_id=pending.occurrence_id,
                        workspace_binding_revision=pending.workspace_binding_revision,
                        managed_permission_categories=pending.managed_permission_categories,
                        skill_refs=pending.skill_refs,
                        automatic_skill_ids=pending.automatic_skill_ids,
                    )
                    handle = self._handles[request.request_id]
                    scheduled_operation = self._run_scheduled(request, handle)
                    task_name = f"run-{request.request_id}"
                self._active_sessions.add(session_id)
                task = asyncio.create_task(scheduled_operation, name=task_name)
                self._scheduled_tasks.add(task)
                task.add_done_callback(self._scheduled_tasks.discard)
                try:
                    handle.attach(task)
                except Exception:
                    task.cancel()
                    self._active_sessions.discard(session_id)
                    if approval is not None:
                        self._resuming_approval_ids.discard(approval.approval_id)
                    raise

    async def _run_scheduled(self, request: RuntimeRequest, handle: RunHandle) -> None:
        if self._provider_runtime is not None:
            async with self._provider_runtime.lease():
                await self._run_scheduled_leased(request, handle)
            return
        await self._run_scheduled_leased(request, handle)

    async def _run_scheduled_leased(
        self,
        request: RuntimeRequest,
        handle: RunHandle,
    ) -> None:
        run_id: str | None = None
        try:
            async with self._coordinator.execution_lock(request.session_id):
                async with self._coordinator.admission_lock(request.session_id):
                    session = self._sessions.load(request.session_id)
                    if session.archived_at is not None:
                        return
                    if not any(
                        item.request_id == request.request_id
                        for item in session.pending_requests
                    ):
                        return
                    _, run_id = self._loop.start_pending_locked(request)
                    control = RunControl()
                    self._controls[run_id] = control
                    self._active_requests[run_id] = request
                    sequence = self._invocation_sequences[request.request_id]
                    handle.publish(
                        self._event(
                            RuntimeEventType.RUN_STARTED,
                            request,
                            sequence,
                            {"run_id": run_id},
                        )
                    )
                usage = await self._loop.run_locked(
                    request,
                    lambda event_type, payload: handle.publish(
                        self._event(event_type, request, sequence, payload)
                    ),
                    already_started=True,
                    on_completed=lambda session, assistant_id: self._complete_run(
                        session, request, assistant_id
                    ),
                    control=control,
                )
                title_usage = await self._apply_automatic_session_title(request)
                if title_usage is not None:
                    usage = _combine_llm_usage(usage, title_usage)
                completed = next(
                    run
                    for run in self._sessions.load(request.session_id).runs
                    if run.run_id == run_id
                )
                handle.publish(
                    self._event(
                        RuntimeEventType.RUN_COMPLETED,
                        request,
                        sequence,
                        {
                            "usage": _usage_payload(usage),
                            "run_id": run_id,
                            "duration_ms": completed.duration_ms,
                            "assistant_message_id": completed.assistant_message_id,
                        },
                    )
                )
        except ApprovalRequired:
            return
        except asyncio.CancelledError:
            await self._mark_terminal(request, RunStatus.CANCELLED)
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_CANCELLED,
                    request,
                    self._invocation_sequences.get(request.request_id, 0),
                )
            )
        except SJTUClawError as exc:
            await self._mark_terminal(request, RunStatus.FAILED)
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_FAILED,
                    request,
                    self._invocation_sequences.get(request.request_id, 0),
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )
            )
        except Exception:
            await self._mark_terminal(request, RunStatus.FAILED)
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_FAILED,
                    request,
                    self._invocation_sequences.get(request.request_id, 0),
                    {
                        "error_type": "UnexpectedRuntimeError",
                        "message": "unexpected runtime failure",
                    },
                )
            )
        finally:
            if run_id is not None:
                self._controls.pop(run_id, None)
                self._active_requests.pop(run_id, None)
            async with self._dispatch_lock:
                self._active_sessions.discard(request.session_id)
                self._queue_session_if_runnable_locked(
                    self._sessions.load(request.session_id)
                )
            await self._dispatch()

    def _terminal_handle(self, request: RuntimeRequest, run: RunState) -> RunHandle:
        sequence = self._invocation_sequences.get(request.request_id, 0)
        handle = RunHandle(self._event(RuntimeEventType.RUN_CANCELLED, request, sequence))
        if run.status is RunStatus.COMPLETED:
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_COMPLETED,
                    request,
                    sequence,
                    {
                        "run_id": run.run_id,
                        "duration_ms": run.duration_ms,
                        "assistant_message_id": run.assistant_message_id,
                    },
                )
            )
        elif run.status in {RunStatus.CANCELLED, RunStatus.CANCELLED_BEFORE_START}:
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_CANCELLED,
                    request,
                    sequence,
                    {"run_id": run.run_id, "code": run.status.value.upper()},
                )
            )
        elif run.status in {RunStatus.FAILED, RunStatus.INTERRUPTED}:
            handle.publish(
                self._event(
                    RuntimeEventType.RUN_FAILED,
                    request,
                    sequence,
                    {"run_id": run.run_id, "code": run.status.value.upper()},
                )
            )
        else:
            raise RuntimeError("active run is missing its runtime handle")
        return handle

    def _complete_run(
        self,
        session: Session,
        request: RuntimeRequest,
        assistant_message_id: str,
    ) -> Session:
        request_id = request.request_id
        completed_at = self._clock.now()
        duration_ms = self._duration_ms(request_id, completed_at)
        return replace(
            session,
            runs=tuple(
                RunState(
                    run_id=run.run_id,
                    request_id=run.request_id,
                    status=(
                        RunStatus.COMPLETED
                        if run.request_id == request_id
                        else run.status
                    ),
                    started_at=run.started_at,
                    completed_at=(
                        completed_at if run.request_id == request_id else run.completed_at
                    ),
                    duration_ms=(duration_ms if run.request_id == request_id else run.duration_ms),
                    assistant_message_id=(
                        assistant_message_id
                        if run.request_id == request_id
                        else run.assistant_message_id
                    ),
                    idempotency_key=run.idempotency_key,
                    selected_skill_refs=run.selected_skill_refs,
                )
                for run in session.runs
            ),
            run_records=self._with_recovered_timing_record(
                session,
                request_id,
                completed_at,
            ),
            skill_usages=tuple(
                replace(usage, output_message_id=assistant_message_id)
                if usage.request_id == request_id
                else usage
                for usage in session.skill_usages
            ),
        )

    async def _apply_automatic_session_title(
        self,
        request: RuntimeRequest,
    ) -> LLMUsage | None:
        if (
            self._session_titles is None
            or request.source is not InvocationSource.GATEWAY
        ):
            return None
        session = self._sessions.load(request.session_id)
        completed_runs = [
            run for run in session.runs if run.status is RunStatus.COMPLETED
        ]
        if (
            session.title != _DEFAULT_SESSION_TITLE
            or len(completed_runs) != 1
            or completed_runs[0].request_id != request.request_id
            or completed_runs[0].assistant_message_id is None
        ):
            return None
        assistant = next(
            (
                message
                for message in session.messages
                if message.message_id == completed_runs[0].assistant_message_id
                and message.role is MessageRole.ASSISTANT
            ),
            None,
        )
        if assistant is None:
            return None
        try:
            result = await self._session_titles.generate(
                request.user_input.text,
                assistant.content,
            )
        except Exception:
            result = SessionTitleResult(
                fallback_session_title(request.user_input.text),
                None,
            )
        current = self._sessions.load(request.session_id)
        if current.title != _DEFAULT_SESSION_TITLE:
            return result.usage
        updated = replace(
            current,
            title=result.title,
            updated_at=self._clock.now(),
        )
        if result.usage is not None:
            updated = accumulate_token_usage(updated, result.usage, self._clock.now())
        self._sessions.save(updated)
        return result.usage

    def _duration_ms(self, request_id: str, completed_at: datetime) -> int:
        submitted_at = self._recovered_submitted_at.get(request_id)
        if submitted_at is not None:
            return max(0, int((completed_at - submitted_at).total_seconds() * 1_000))
        started_ms = self._submitted_monotonic_ms.get(
            request_id,
            self._clock.monotonic_ms(),
        )
        return max(0, self._clock.monotonic_ms() - started_ms)

    def _with_recovered_timing_record(
        self,
        session: Session,
        request_id: str,
        completed_at: datetime,
    ) -> tuple[RunRecord, ...]:
        submitted_at = self._recovered_submitted_at.get(request_id)
        if submitted_at is None:
            return session.run_records
        timing_is_valid = completed_at >= submitted_at
        code = (
            "RUN_RECOVERED_TIMING"
            if timing_is_valid
            else "RUN_RECOVERED_TIMING_UNAVAILABLE"
        )
        message = (
            "duration includes persisted queue time after runtime recovery"
            if timing_is_valid
            else "recovered timing unavailable because system time moved backwards"
        )
        sequence = (
            1
            if not session.run_records
            else session.run_records[-1].sequence_number + 1
        )
        return (
            *session.run_records,
            RunRecord(
                record_id=self._ids.new_id("record"),
                invocation_id=request_id,
                record_type=RunRecordType.STATUS,
                code=code,
                message=message,
                created_at=completed_at,
                sequence_number=sequence,
            ),
        )

    async def _mark_terminal(self, request: RuntimeRequest, status: RunStatus) -> None:
        try:
            async with self._coordinator.admission_lock(request.session_id):
                session = self._sessions.load(request.session_id)
                now = self._clock.now()
                duration_ms = self._duration_ms(request.request_id, now)
                pending = session.pending_approval
                self._sessions.save(
                    replace(
                        session,
                        pending_approval=(
                            None
                            if pending is not None
                            and pending.invocation_id == request.request_id
                            else pending
                        ),
                        runs=tuple(
                            RunState(
                                run_id=run.run_id,
                                request_id=run.request_id,
                                status=(
                                    status
                                    if run.request_id == request.request_id
                                    else run.status
                                ),
                                started_at=run.started_at,
                                completed_at=(
                                    now
                                    if run.request_id == request.request_id
                                    else run.completed_at
                                ),
                                duration_ms=(
                                    duration_ms
                                    if run.request_id == request.request_id
                                    else run.duration_ms
                                ),
                                assistant_message_id=run.assistant_message_id,
                                idempotency_key=run.idempotency_key,
                                selected_skill_refs=run.selected_skill_refs,
                            )
                            for run in session.runs
                        ),
                        updated_at=now,
                    )
                )
        except Exception:
            return
