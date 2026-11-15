"""Locked multi-step agent loop with safe tool execution and audit records."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from sjtuclaw.errors import AgentLimitError, ApprovalRequired, LLMError, SJTUClawError, SkillError
from sjtuclaw.models.compaction import CompactionResult, CompactionStatus
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMStreamEvent, LLMToolMode, LLMUsage
from sjtuclaw.models.message import Message, MessageRole, UserInputKind
from sjtuclaw.models.run_record import RunRecord, RunRecordType
from sjtuclaw.models.runtime import InvocationSource, RuntimeEventType, RuntimeRequest
from sjtuclaw.models.session import (
    ApprovalDecision,
    ApprovalDecisionStatus,
    PendingApproval,
    RunCheckpoint,
    RunState,
    RunStatus,
    Session,
)
from sjtuclaw.models.skill import SkillReference, SkillUsage, SkillUsageSource
from sjtuclaw.models.tool import (
    PermissionDecision,
    ToolCall,
    ToolExecutionContext,
    ToolIntent,
    ToolResourceLink,
    ToolResult,
    ToolResultStatus,
    mutable_json,
)
from sjtuclaw.ports.llm import LLMClient, StreamingLLMClient
from sjtuclaw.ports.runtime import Clock, IdGenerator
from sjtuclaw.ports.stores import SessionStore
from sjtuclaw.ports.tools import ToolBatchValidationError, ToolExecutor
from sjtuclaw.runtime.compaction import CompactionService
from sjtuclaw.runtime.context_builder import ContextBuilder
from sjtuclaw.runtime.memory_capture import MemoryCapture
from sjtuclaw.runtime.permission import PermissionPolicy
from sjtuclaw.runtime.run_control import RunControl
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_state import merge_active_session
from sjtuclaw.runtime.skill_service import SkillService
from sjtuclaw.runtime.token_usage import accumulate_token_usage

EmitEvent = Callable[[RuntimeEventType, dict[str, object]], None]
CompleteRun = Callable[[Session, str], Session]

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|secret)\b\s*[:=]\s*\S+"
)
_AUTHORIZATION_VALUE = re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[^\r\n,;]+")
logger = logging.getLogger(__name__)


class AgentLoop:
    """Run one user invocation while its session coordinator lock is held."""

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
        if (tools is None) != (permission is None):
            raise ValueError("tools and permission must be supplied together")
        if tools is None and tool_mode is not None:
            raise ValueError("tool_mode requires tools and permission")
        if tools is not None and tool_mode is None:
            raise ValueError("tools and permission require tool_mode")
        if max_steps < 0:
            raise ValueError("max_steps must be non-negative")
        if repeat_limit < 1:
            raise ValueError("repeat_limit must be positive")

        self._llm = llm
        self._sessions = sessions
        self._context_builder = context_builder
        self._coordinator = coordinator
        self._clock = clock
        self._ids = ids
        self._compaction = compaction
        self._tools = tools
        self._permission = permission
        self._tool_mode = tool_mode
        self._max_steps = max_steps
        self._repeat_limit = repeat_limit
        self._skills = skills
        self._memory_capture = memory_capture

    async def run_locked(
        self,
        request: RuntimeRequest,
        emit: EmitEvent,
        *,
        already_started: bool = False,
        on_completed: CompleteRun | None = None,
        control: RunControl | None = None,
    ) -> LLMUsage:
        """Persist one complete tool-aware invocation; caller owns session lock."""

        session: Session | None = None
        try:
            session = self._sessions.load(request.session_id)
            if not already_started:
                session = self._append_user_message(session, request)
                session = await self._save_active_session(session)

            self._emit_initial_skill_selections(session, request, emit)
            emit(RuntimeEventType.RUN_PROGRESS, {"code": "context.preparing"})
            if self._compaction is not None:
                session, compaction_result = await self._compaction.maybe_compact_locked(
                    session
                )
                self._emit_compaction(emit, compaction_result)

            return await self._run_steps(session, request, emit, on_completed, control)
        except asyncio.CancelledError:
            if session is not None:
                await self._close_tool_context(session, request)
                await self._record_terminal_best_effort(
                    request,
                    RunRecordType.STATUS,
                    "RUN_CANCELLED",
                    "agent run cancelled",
                )
            raise
        except ApprovalRequired:
            raise
        except SJTUClawError as error:
            if session is not None:
                await self._close_tool_context(session, request)
                await self._record_terminal_best_effort(
                    request,
                    RunRecordType.ERROR,
                    _error_code(error),
                    _safe_error_message(error),
                )
            raise
        except Exception:
            if session is not None:
                await self._close_tool_context(session, request)
                await self._record_terminal_best_effort(
                    request,
                    RunRecordType.ERROR,
                    "UNEXPECTED_RUNTIME_ERROR",
                    "unexpected runtime failure",
                )
            raise

    async def _run_steps(
        self,
        session: Session,
        request: RuntimeRequest,
        emit: EmitEvent,
        on_completed: CompleteRun | None,
        control: RunControl | None,
    ) -> LLMUsage:
        steps = 0
        last_signature: str | None = None
        consecutive_calls = 0
        while True:
            session, applied = await self._apply_pending_steer(session, emit, control)
            if applied:
                steps = 0
                last_signature = None
                consecutive_calls = 0
                continue
            request_for_model = LLMRequest(
                messages=self._context_builder.build(session, skill_refs=request.skill_refs),
                tools=() if self._tools is None else self._tools.definitions(),
                tool_mode=self._tool_mode,
            )
            emit(RuntimeEventType.RUN_PROGRESS, {"code": "model.started"})
            response = await self._respond(request_for_model, emit, control)
            if response is None:
                session, _ = await self._apply_pending_steer(session, emit, control)
                steps = 0
                last_signature = None
                consecutive_calls = 0
                continue
            steps += 1
            response_time = self._clock.now()
            session = accumulate_token_usage(session, response.usage, response_time)

            session, applied = await self._apply_pending_steer(session, emit, control)
            if applied:
                steps = 0
                last_signature = None
                consecutive_calls = 0
                continue

            if not response.tool_calls:
                emit(RuntimeEventType.RUN_PROGRESS, {"code": "model.synthesizing"})
                session = self._append_assistant_message(
                    session,
                    response.content,
                    response_time,
                    reasoning_content=response.reasoning_content,
                )
                await self._close_tool_context(session, request)
                if on_completed is not None:
                    session = on_completed(session, session.messages[-1].message_id)
                session = self._append_record(
                    session,
                    request,
                    RunRecordType.STATUS,
                    "RUN_COMPLETED",
                    "agent run completed",
                )
                session = await self._save_active_session(session)
                usage_payload = _usage_payload(response.usage)
                emit(
                    RuntimeEventType.ASSISTANT_MESSAGE,
                    {"content": response.content, "usage": usage_payload},
                )
                if (
                    self._memory_capture is not None
                    and request.source
                    in {InvocationSource.CLI, InvocationSource.GATEWAY}
                ):
                    try:
                        await self._memory_capture.capture(session, request)
                    except Exception as error:
                        logger.warning(
                            "automatic memory capture failed after %s",
                            type(error).__name__,
                        )
                return response.usage

            session = self._append_assistant_message(
                session,
                response.content,
                response_time,
                tool_calls=response.tool_calls,
                reasoning_content=response.reasoning_content,
            )
            session = await self._save_active_session(session)
            for call in response.tool_calls:
                emit(
                    RuntimeEventType.TOOL_CALL,
                    {
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                )

            if self._tools is None or self._permission is None:
                raise AgentLimitError("model requested tools when tool execution is disabled")

            try:
                intents = self._tools.validate_batch(response.tool_calls)
            except ToolBatchValidationError as error:
                last_signature = None
                consecutive_calls = 0
                session = await self._persist_results(
                    session,
                    request,
                    error.results,
                    emit,
                )
            else:
                steer_applied = False
                calls_by_id = {call.call_id: call for call in response.tool_calls}
                for index, intent in enumerate(intents):
                    if control is not None and control.has_pending():
                        interruptions = tuple(
                            ToolResult(
                                call_id=remaining.call_id,
                                tool_name=remaining.name,
                                content="tool call interrupted by steer before execution",
                                status=ToolResultStatus.ERROR,
                                error_code="STEER_INTERRUPTED",
                            )
                            for remaining in intents[index:]
                        )
                        session = await self._persist_results(
                            session,
                            request,
                            interruptions,
                            emit,
                        )
                        session, steer_applied = await self._apply_pending_steer(
                            session,
                            emit,
                            control,
                        )
                        break
                    signature = _tool_signature(intent)
                    if signature == last_signature:
                        consecutive_calls += 1
                    else:
                        last_signature = signature
                        consecutive_calls = 1

                    if consecutive_calls > self._repeat_limit:
                        result = ToolResult(
                            call_id=intent.call_id,
                            tool_name=intent.name,
                            content="repeated tool call blocked by agent safety limit",
                            status=ToolResultStatus.ERROR,
                            error_code="REPEATED_TOOL_CALL",
                        )
                        session = await self._persist_results(
                            session,
                            request,
                            (result,),
                            emit,
                        )
                        continue

                    decision = self._permission.decide(
                        intent,
                        self._tool_context(session, request),
                    )
                    session = self._append_record(
                        session,
                        request,
                        RunRecordType.PERMISSION,
                        f"PERMISSION_{decision.value.upper()}",
                        _permission_message(decision),
                        tool_call_id=intent.call_id,
                        tool_name=intent.name,
                    )
                    session = await self._save_active_session(session)
                    if decision is PermissionDecision.ASK:
                        if request.source is InvocationSource.SCHEDULER:
                            result = ToolResult(
                                call_id=intent.call_id,
                                tool_name=intent.name,
                                content="scheduled task lacks the managed permission",
                                status=ToolResultStatus.ERROR,
                                error_code="SCHEDULED_PERMISSION_DENIED",
                            )
                            session = await self._persist_results(
                                session,
                                request,
                                (result,),
                                emit,
                            )
                            continue
                        approval = self._pause_for_approval(
                            session,
                            request,
                            calls_by_id[intent.call_id],
                            intent,
                            index,
                        )
                        session = await self._save_active_session(approval)
                        emit(
                            RuntimeEventType.APPROVAL_REQUIRED,
                            {
                                "approval_id": approval.pending_approval.approval_id
                                if approval.pending_approval is not None
                                else "",
                                "tool": intent.name,
                                "safety_level": intent.safety_level.value,
                            },
                        )
                        raise ApprovalRequired(
                            approval.pending_approval.approval_id
                            if approval.pending_approval is not None
                            else "approval_missing"
                        )
                    if decision is not PermissionDecision.ALLOW:
                        result = ToolResult(
                            call_id=intent.call_id,
                            tool_name=intent.name,
                            content=_permission_result_message(decision),
                            status=ToolResultStatus.ERROR,
                            error_code=_permission_result_code(decision),
                        )
                        session = await self._persist_results(
                            session,
                            request,
                            (result,),
                            emit,
                        )
                        continue

                    authorized = self._permission.authorize(
                        intent,
                        self._tool_context(session, request),
                    )
                    if intent.name == "select_skill":
                        session, request, result = await self._select_skill(
                            session,
                            request,
                            intent,
                            emit,
                        )
                        session = await self._persist_results(session, request, (result,), emit)
                        continue

                    emit(
                        RuntimeEventType.RUN_PROGRESS,
                        {"code": "tool.running", "tool": intent.name},
                    )
                    results = await self._tools.execute_batch(
                        (authorized,),
                        context=self._tool_context(session, request),
                    )
                    if intent.name == "read_skill_resource" and not results[0].is_error:
                        session = self._record_skill_resource_usage(session, request, intent)
                    session = await self._persist_results(session, request, results, emit)

                if steer_applied:
                    steps = 0
                    last_signature = None
                    consecutive_calls = 0
                    continue

            session, applied = await self._apply_pending_steer(session, emit, control)
            if applied:
                steps = 0
                last_signature = None
                consecutive_calls = 0
                continue

            if self._max_steps and steps >= self._max_steps:
                raise AgentLimitError("MAX_STEPS_REACHED")

    async def resume_approval_locked(
        self,
        request: RuntimeRequest,
        approval: PendingApproval,
        decision: ApprovalDecision,
        emit: EmitEvent,
        *,
        on_completed: CompleteRun | None = None,
        control: RunControl | None = None,
    ) -> LLMUsage:
        """Persist one decision/result, then continue the same started Run."""

        session = self._sessions.load(request.session_id)
        if session.pending_approval != approval:
            raise ValueError("approval is no longer pending")
        if self._tools is None or self._permission is None:
            raise AgentLimitError("approval resumed without tool support")
        run = next((item for item in session.runs if item.run_id == approval.run_id), None)
        if run is None or run.checkpoint is None:
            raise ValueError("approval run checkpoint is unavailable")
        checkpoint = run.checkpoint
        try:
            intents = self._tools.validate_batch((approval.tool_call,))
        except ToolBatchValidationError as error:
            results = error.results
        else:
            if intents != (approval.intent,):
                results = (
                    ToolResult(
                        call_id=approval.tool_call.call_id,
                        tool_name=approval.tool_call.name,
                        content="approved tool context changed before execution",
                        status=ToolResultStatus.ERROR,
                        error_code="APPROVAL_CONTEXT_CHANGED",
                    ),
                )
            elif decision.status is ApprovalDecisionStatus.APPROVED:
                # This durable marker is written before the side effect begins.
                # Recovery must terminate instead of replaying a possibly completed
                # write/command whose ToolResult never reached the Session store.
                session = self._mark_approved_execution_started(session, approval)
                session = await self._save_active_session(session)
                authorized = self._permission.authorize_approved(approval.intent)
                emit(
                    RuntimeEventType.RUN_PROGRESS,
                    {"code": "tool.running", "tool": approval.intent.name},
                )
                results = await self._tools.execute_batch(
                    (authorized,),
                    context=self._tool_context(session, request),
                )
            else:
                results = (
                    ToolResult(
                        call_id=approval.tool_call.call_id,
                        tool_name=approval.tool_call.name,
                        content=_approval_rejection_message(decision),
                        status=ToolResultStatus.ERROR,
                        error_code=(
                            "APPROVAL_TIMEOUT"
                            if decision.status is ApprovalDecisionStatus.TIMED_OUT
                            else "APPROVAL_DENIED"
                        ),
                    ),
                )
        session = await self._persist_results(session, request, results, emit)
        session = await self._resume_remaining_approved_batch(
            session,
            request,
            approval,
            checkpoint,
            emit,
        )
        return await self._run_steps(session, request, emit, on_completed, control)

    async def _resume_remaining_approved_batch(
        self,
        session: Session,
        request: RuntimeRequest,
        approval: PendingApproval,
        checkpoint: RunCheckpoint,
        emit: EmitEvent,
    ) -> Session:
        """Continue unexecuted calls from the assistant batch in their saved order."""

        if self._tools is None or self._permission is None:
            raise AgentLimitError("approval resumed without tool support")
        batch = self._approval_batch(session, approval, checkpoint)
        try:
            intents = self._tools.validate_batch(batch)
        except ToolBatchValidationError:
            # The approved call already has a durable observation.  A corrupted
            # historical batch must not manufacture or execute later calls.
            return session
        if (
            checkpoint.next_tool_call_index >= len(intents)
            or intents[checkpoint.next_tool_call_index] != approval.intent
        ):
            return session

        for index in range(checkpoint.next_tool_call_index + 1, len(intents)):
            intent = intents[index]
            decision = self._permission.decide(
                intent,
                self._tool_context(session, request),
            )
            session = self._append_record(
                session,
                request,
                RunRecordType.PERMISSION,
                f"PERMISSION_{decision.value.upper()}",
                _permission_message(decision),
                tool_call_id=intent.call_id,
                tool_name=intent.name,
            )
            session = await self._save_active_session(session)
            if decision is PermissionDecision.ASK:
                if request.source is InvocationSource.SCHEDULER:
                    result = ToolResult(
                        call_id=intent.call_id,
                        tool_name=intent.name,
                        content="scheduled task lacks the managed permission",
                        status=ToolResultStatus.ERROR,
                        error_code="SCHEDULED_PERMISSION_DENIED",
                    )
                    session = await self._persist_results(session, request, (result,), emit)
                    continue
                paused = self._pause_for_approval(
                    session,
                    request,
                    batch[index],
                    intent,
                    index,
                )
                session = await self._save_active_session(paused)
                emit(
                    RuntimeEventType.APPROVAL_REQUIRED,
                    {
                        "approval_id": (
                            paused.pending_approval.approval_id
                            if paused.pending_approval is not None
                            else ""
                        ),
                        "tool": intent.name,
                        "safety_level": intent.safety_level.value,
                    },
                )
                raise ApprovalRequired(
                    paused.pending_approval.approval_id
                    if paused.pending_approval is not None
                    else "approval_missing"
                )
            if decision is not PermissionDecision.ALLOW:
                result = ToolResult(
                    call_id=intent.call_id,
                    tool_name=intent.name,
                    content=_permission_result_message(decision),
                    status=ToolResultStatus.ERROR,
                    error_code=_permission_result_code(decision),
                )
                session = await self._persist_results(session, request, (result,), emit)
                continue
            authorized = self._permission.authorize(
                intent,
                self._tool_context(session, request),
            )
            emit(
                RuntimeEventType.RUN_PROGRESS,
                {"code": "tool.running", "tool": intent.name},
            )
            results = await self._tools.execute_batch(
                (authorized,),
                context=self._tool_context(session, request),
            )
            session = await self._persist_results(session, request, results, emit)
        return session

    @staticmethod
    def _approval_batch(
        session: Session,
        approval: PendingApproval,
        checkpoint: RunCheckpoint,
    ) -> tuple[ToolCall, ...]:
        for message in reversed(session.messages):
            if (
                message.role is MessageRole.ASSISTANT
                and len(message.tool_calls) > checkpoint.next_tool_call_index
                and message.tool_calls[checkpoint.next_tool_call_index]
                == approval.tool_call
            ):
                return message.tool_calls
        raise ValueError("approval assistant tool batch is unavailable")

    async def _respond(
        self,
        request: LLMRequest,
        emit: EmitEvent,
        control: RunControl | None,
    ) -> LLMResponse | None:
        if not isinstance(self._llm, StreamingLLMClient):
            return await self._llm.complete(request)

        response: LLMResponse | None = None
        stream = self._llm.stream(request)
        while True:
            next_event: asyncio.Future[LLMStreamEvent] = asyncio.ensure_future(anext(stream))
            pending: asyncio.Task[None] | None = (
                None
                if control is None
                else asyncio.create_task(control.wait_for_pending())
            )
            wait_for = (next_event,) if pending is None else (next_event, pending)
            done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
            if pending is not None and pending in done:
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
                return None
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            try:
                event = next_event.result()
            except StopAsyncIteration:
                break
            if event.content_delta is not None:
                emit(RuntimeEventType.ASSISTANT_DELTA, {"delta": event.content_delta})
                continue
            if response is not None or event.response is None:
                raise LLMError("LLM stream returned an invalid completion sequence")
            response = event.response
        if response is None:
            raise LLMError("LLM stream ended without a completed response")
        return response

    def _append_user_message(self, session: Session, request: RuntimeRequest) -> Session:
        now = self._clock.now()
        message = Message(
            message_id=self._ids.new_id("message"),
            role=MessageRole.USER,
            content=request.user_input.text,
            created_at=now,
            sequence_number=session.next_sequence_number,
            attachment_refs=request.attachment_refs,
        )
        return replace(
            session,
            messages=(*session.messages, message),
            updated_at=now,
            next_sequence_number=session.next_sequence_number + 1,
        )

    def _pause_for_approval(
        self,
        session: Session,
        request: RuntimeRequest,
        tool_call: ToolCall,
        intent: ToolIntent,
        next_tool_call_index: int,
    ) -> Session:
        if session.pending_approval is not None:
            raise ValueError("session already has a pending approval")
        run = next((item for item in session.runs if item.request_id == request.request_id), None)
        if run is None or run.status is not RunStatus.STARTED:
            raise ValueError("approval requires a started run")
        now = self._clock.now()
        approval_id = self._ids.new_id("approval")
        checkpoint = RunCheckpoint(
            checkpoint_id=self._ids.new_id("checkpoint"),
            run_id=run.run_id,
            request_id=request.request_id,
            pending_approval_id=approval_id,
            next_tool_call_index=next_tool_call_index,
            created_at=now,
            source=request.source,
            actor_id=request.actor_id,
            input_text=request.user_input.text,
            attachment_refs=request.attachment_refs,
            skill_refs=request.skill_refs,
            automatic_skill_ids=request.automatic_skill_ids,
        )
        binding = session.workspace_binding
        approval = PendingApproval(
            approval_id=approval_id,
            run_id=run.run_id,
            invocation_id=request.request_id,
            tool_call=tool_call,
            intent=intent,
            reason=f"{intent.name} requires one-time approval",
            created_at=now,
            expires_at=now + timedelta(seconds=1_800),
            workspace_binding_id=None if binding is None else binding.binding_id,
            workspace_revision=None if binding is None else binding.revision,
        )
        return replace(
            session,
            runs=tuple(
                replace(item, checkpoint=checkpoint)
                if item.request_id == request.request_id
                else item
                for item in session.runs
            ),
            pending_approval=approval,
            updated_at=now,
        )

    @staticmethod
    def _mark_approved_execution_started(
        session: Session,
        approval: PendingApproval,
    ) -> Session:
        if session.pending_approval != approval:
            raise ValueError("approval is no longer pending")
        return replace(
            session,
            runs=tuple(
                replace(
                    run,
                    checkpoint=replace(
                        run.checkpoint,
                        approved_execution_started=True,
                    ),
                )
                if run.run_id == approval.run_id and run.checkpoint is not None
                else run
                for run in session.runs
            ),
        )

    @staticmethod
    def _tool_context(session: Session, request: RuntimeRequest) -> ToolExecutionContext:
        binding = session.workspace_binding
        return ToolExecutionContext(
            session_id=request.session_id,
            request_id=request.request_id,
            source=request.source,
            actor_id=request.actor_id,
            workspace_binding_id=None if binding is None else binding.binding_id,
            workspace_revision=None if binding is None else binding.revision,
            workspace_root=None if binding is None else Path(binding.canonical_root),
            selected_skill_snapshot_ids=tuple(
                reference.snapshot_id for reference in request.skill_refs
            ),
            selected_skill_refs=request.skill_refs,
            scheduler_task_id=request.scheduler_task_id,
            occurrence_id=request.occurrence_id,
            scheduler_workspace_binding_revision=request.workspace_binding_revision,
            managed_permission_categories=request.managed_permission_categories,
        )

    async def _apply_pending_steer(
        self,
        session: Session,
        emit: EmitEvent,
        control: RunControl | None,
    ) -> tuple[Session, bool]:
        if control is None or not control.has_pending():
            return session, False
        command = await control.take_pending()
        if command is None:
            return session, False
        now = self._clock.now()
        message = Message(
            message_id=self._ids.new_id("message"),
            role=MessageRole.USER,
            content=command.text,
            created_at=now,
            sequence_number=session.next_sequence_number,
            input_kind=UserInputKind.STEER,
        )
        updated = replace(
            session,
            messages=(*session.messages, message),
            updated_at=now,
            next_sequence_number=session.next_sequence_number + 1,
        )
        updated = await self._save_active_session(updated)
        emit(RuntimeEventType.STEER_APPLIED, {"steer_id": command.steer_id})
        return updated, True

    def start_pending_locked(self, request: RuntimeRequest) -> tuple[Session, str]:
        """Atomically consume a persisted request before any LLM or Tool work begins."""

        session = self._sessions.load(request.session_id)
        pending = next(
            (
                item
                for item in session.pending_requests
                if item.request_id == request.request_id
            ),
            None,
        )
        if pending is None:
            raise ValueError("pending request not found")
        if (
            pending.text != request.user_input.text
            or pending.source is not request.source
            or pending.actor_id != request.actor_id
            or pending.attachment_refs != request.attachment_refs
            or pending.idempotency_key != request.idempotency_key
            or pending.scheduler_task_id != request.scheduler_task_id
            or pending.occurrence_id != request.occurrence_id
            or pending.workspace_binding_revision != request.workspace_binding_revision
            or pending.managed_permission_categories != request.managed_permission_categories
            or pending.skill_refs != request.skill_refs
            or pending.automatic_skill_ids != request.automatic_skill_ids
        ):
            raise ValueError("pending request does not match runtime request")

        started = replace(
            self._append_user_message(session, request),
            explicit_skill_refs=tuple(
                reference
                for reference in request.skill_refs
                if reference.skill_id not in request.automatic_skill_ids
            ),
            skill_usages=(
                *session.skill_usages,
                *self._initial_skill_usages(request),
            ),
        )
        now = self._clock.now()
        run_id: str | None = None
        runs: list[RunState] = []
        for run in started.runs:
            if run.request_id == request.request_id and run.status is RunStatus.QUEUED:
                run_id = run.run_id
                runs.append(
                    RunState(
                        run_id=run.run_id,
                        request_id=run.request_id,
                        status=RunStatus.STARTED,
                        started_at=now,
                        idempotency_key=run.idempotency_key,
                        selected_skill_refs=run.selected_skill_refs,
                    )
                )
            else:
                runs.append(run)
        if run_id is None:
            raise ValueError("queued run not found")
        started = replace(
            started,
            pending_requests=tuple(
                item
                for item in started.pending_requests
                if item.request_id != request.request_id
            ),
            runs=tuple(runs),
        )
        self._sessions.save(started)
        return started, run_id

    def _initial_skill_usages(self, request: RuntimeRequest) -> tuple[SkillUsage, ...]:
        explicit_source = (
            SkillUsageSource.SCHEDULED_EXPLICIT
            if request.source is InvocationSource.SCHEDULER
            else SkillUsageSource.EXPLICIT
        )
        return tuple(
            SkillUsage(
                reference=reference,
                source=(
                    SkillUsageSource.AUTO
                    if reference.skill_id in request.automatic_skill_ids
                    else explicit_source
                ),
                selection_order=index,
                reason=(
                    f"automatic {reference.skill_id} routing"
                    if reference.skill_id in request.automatic_skill_ids
                    else "explicit local Skill reference"
                ),
                selected_at=self._clock.now(),
                request_id=request.request_id,
                user_task=request.user_input.text,
            )
            for index, reference in enumerate(request.skill_refs, start=1)
        )

    @staticmethod
    def _emit_initial_skill_selections(
        session: Session,
        request: RuntimeRequest,
        emit: EmitEvent,
    ) -> None:
        explicit_source = (
            SkillUsageSource.SCHEDULED_EXPLICIT
            if request.source is InvocationSource.SCHEDULER
            else SkillUsageSource.EXPLICIT
        )
        usages = {
            (usage.reference.snapshot_id, usage.source): usage
            for usage in session.skill_usages
            if usage.request_id == request.request_id
        }
        for reference in request.skill_refs:
            source = (
                SkillUsageSource.AUTO
                if reference.skill_id in request.automatic_skill_ids
                else explicit_source
            )
            usage = usages.get((reference.snapshot_id, source))
            emit(
                RuntimeEventType.RUN_PROGRESS,
                {
                    "code": "skill.selected",
                    "skill_id": reference.skill_id,
                    "source": source.value,
                    "reason": (
                        usage.reason
                        if usage is not None
                        else (
                            f"automatic {reference.skill_id} routing"
                            if source is SkillUsageSource.AUTO
                            else "explicit local Skill reference"
                        )
                    ),
                },
            )

    def _append_assistant_message(
        self,
        session: Session,
        content: str,
        created_at: datetime,
        *,
        tool_calls: tuple[ToolCall, ...] = (),
        reasoning_content: str | None = None,
    ) -> Session:
        message = Message(
            message_id=self._ids.new_id("message"),
            role=MessageRole.ASSISTANT,
            content=content,
            created_at=created_at,
            sequence_number=session.next_sequence_number,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )
        return replace(
            session,
            messages=(*session.messages, message),
            updated_at=created_at,
            next_sequence_number=session.next_sequence_number + 1,
        )

    async def _persist_results(
        self,
        session: Session,
        request: RuntimeRequest,
        results: tuple[ToolResult, ...],
        emit: EmitEvent,
    ) -> Session:
        for result in results:
            now = self._clock.now()
            message = Message(
                message_id=self._ids.new_id("message"),
                role=MessageRole.TOOL,
                content=result.content,
                created_at=now,
                sequence_number=session.next_sequence_number,
                tool_call_id=result.call_id,
                tool_name=result.tool_name,
                tool_result_status=result.status,
                tool_error_code=result.error_code,
                tool_structured_content=result.structured_content,
                tool_resources=result.resources,
            )
            session = replace(
                session,
                messages=(*session.messages, message),
                updated_at=now,
                next_sequence_number=session.next_sequence_number + 1,
            )
            # This is the commit point for an approved side effect.  Persisting
            # the observation and clearing its checkpoint together means a
            # restart can distinguish an unstarted approved call from one that
            # may already have run.
            session = self._clear_resolved_approval(
                session,
                request.request_id,
                result.call_id,
            )
            session = await self._save_active_session(session)
            if result.is_error:
                session = self._append_record(
                    session,
                    request,
                    RunRecordType.ERROR,
                    result.error_code or "TOOL_ERROR",
                    _safe_observation(result.content),
                    tool_call_id=result.call_id,
                    tool_name=result.tool_name,
                )
                session = await self._save_active_session(session)
            emit(
                RuntimeEventType.TOOL_RESULT,
                {
                    "call_id": result.call_id,
                    "tool": result.tool_name,
                    "content": result.content,
                    "status": result.status.value,
                    "error_code": result.error_code,
                    "structured_content": (
                        mutable_json(result.structured_content)
                        if result.structured_content is not None
                        else None
                    ),
                    "resources": [
                        _resource_payload(resource) for resource in result.resources
                    ],
                },
            )
        return session

    async def _select_skill(
        self,
        session: Session,
        request: RuntimeRequest,
        intent: ToolIntent,
        emit: EmitEvent,
    ) -> tuple[Session, RuntimeRequest, ToolResult]:
        """Accept only the structured selection Tool, never natural-language guesses."""

        if self._skills is None:
            return (
                session,
                request,
                ToolResult(
                    call_id=intent.call_id,
                    tool_name=intent.name,
                    content="local Skill selection is unavailable",
                    status=ToolResultStatus.ERROR,
                    error_code="SKILLS_UNAVAILABLE",
                ),
            )
        skill_id = intent.arguments.get("skill_id")
        reason = intent.arguments.get("reason")
        if not isinstance(skill_id, str) or not isinstance(reason, str):
            return (
                session,
                request,
                ToolResult(
                    call_id=intent.call_id,
                    tool_name=intent.name,
                    content="structured Skill selection is invalid",
                    status=ToolResultStatus.ERROR,
                    error_code="SKILL_SELECTION_INVALID",
                ),
            )
        try:
            reference = self._skills.resolve_current_names((skill_id,))[0]
        except SkillError as exc:
            return (
                session,
                request,
                ToolResult(
                    call_id=intent.call_id,
                    tool_name=intent.name,
                    content="requested local Skill is unavailable",
                    status=ToolResultStatus.ERROR,
                    error_code=exc.code,
                ),
            )
        if any(item.skill_id == reference.skill_id for item in request.skill_refs):
            structured_content = _skill_reference_payload(reference)
            return (
                session,
                request,
                ToolResult(
                    call_id=intent.call_id,
                    tool_name=intent.name,
                    content=f"local Skill ${reference.skill_id} is already selected",
                    structured_content=structured_content,
                ),
            )
        if len(request.skill_refs) >= 3:
            return (
                session,
                request,
                ToolResult(
                    call_id=intent.call_id,
                    tool_name=intent.name,
                    content="a Run may select at most three different local Skills",
                    status=ToolResultStatus.ERROR,
                    error_code="SKILL_SELECTION_LIMIT",
                ),
            )
        selected = (*request.skill_refs, reference)
        selected_request = replace(request, skill_refs=selected)
        updated = self._record_auto_skill_selection(session, selected_request, reference, reason)
        updated = await self._save_active_session(updated)
        emit(
            RuntimeEventType.RUN_PROGRESS,
            {
                "code": "skill.selected",
                "skill_id": reference.skill_id,
                "source": "auto",
                "reason": reason,
            },
        )
        return (
            updated,
            selected_request,
            ToolResult(
                call_id=intent.call_id,
                tool_name=intent.name,
                content=(
                    f"selected local Skill ${reference.skill_id} v{reference.version}; "
                    "its instruction layer applies on the next model step"
                ),
                structured_content=_skill_reference_payload(reference),
            ),
        )

    def _record_auto_skill_selection(
        self,
        session: Session,
        request: RuntimeRequest,
        reference: SkillReference,
        reason: str,
    ) -> Session:
        updated_runs = tuple(
            replace(run, selected_skill_refs=request.skill_refs)
            if run.request_id == request.request_id
            else run
            for run in session.runs
        )
        usage = SkillUsage(
            reference=reference,
            source=SkillUsageSource.AUTO,
            selection_order=len(request.skill_refs),
            reason=reason.strip()[:500] or "model selected local Skill",
            selected_at=self._clock.now(),
            request_id=request.request_id,
            user_task=request.user_input.text,
        )
        return replace(session, runs=updated_runs, skill_usages=(*session.skill_usages, usage))

    @staticmethod
    def _record_skill_resource_usage(
        session: Session,
        request: RuntimeRequest,
        intent: ToolIntent,
    ) -> Session:
        skill_id = intent.arguments.get("skill_id")
        path = intent.arguments.get("path")
        if not isinstance(skill_id, str) or not isinstance(path, str):
            return session
        usages = list(session.skill_usages)
        for index in range(len(usages) - 1, -1, -1):
            usage = usages[index]
            if usage.request_id != request.request_id or usage.reference.skill_id != skill_id:
                continue
            resource_paths = usage.resource_paths
            if path not in resource_paths:
                resource_paths = (*resource_paths, path)
            usages[index] = replace(usage, resource_paths=resource_paths)
            return replace(session, skill_usages=tuple(usages))
        return session

    @staticmethod
    def _clear_resolved_approval(
        session: Session,
        request_id: str,
        tool_call_id: str,
    ) -> Session:
        pending = session.pending_approval
        if (
            pending is None
            or pending.invocation_id != request_id
            or pending.tool_call.call_id != tool_call_id
        ):
            return session
        return replace(
            session,
            pending_approval=None,
            runs=tuple(
                replace(run, checkpoint=None)
                if run.request_id == request_id
                and run.checkpoint is not None
                and run.checkpoint.pending_approval_id == pending.approval_id
                else run
                for run in session.runs
            ),
        )

    def _append_record(
        self,
        session: Session,
        request: RuntimeRequest,
        record_type: RunRecordType,
        code: str,
        message: str,
        *,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> Session:
        sequence = 1 if not session.run_records else session.run_records[-1].sequence_number + 1
        now = self._clock.now()
        record = RunRecord(
            record_id=self._ids.new_id("record"),
            invocation_id=request.request_id,
            record_type=record_type,
            code=code,
            message=_safe_observation(message),
            created_at=now,
            sequence_number=sequence,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        return replace(
            session,
            run_records=(*session.run_records, record),
            updated_at=now,
        )

    async def _record_terminal_best_effort(
        self,
        request: RuntimeRequest,
        record_type: RunRecordType,
        code: str,
        message: str,
    ) -> None:
        """Append terminal audit record without replacing a newer saved session."""

        try:
            current = self._sessions.load(request.session_id)
            await self._save_active_session(
                self._append_record(current, request, record_type, code, message)
            )
        except Exception:
            return

    @staticmethod
    def _emit_compaction(emit: EmitEvent, result: CompactionResult) -> None:
        if result.status is CompactionStatus.COMPACTED:
            emit(RuntimeEventType.COMPACTION_COMPLETED, _compaction_payload(result))
        elif result.status is CompactionStatus.FAILED:
            emit(RuntimeEventType.COMPACTION_FAILED, _compaction_payload(result))

    async def _save_active_session(self, session: Session) -> Session:
        async with self._coordinator.admission_lock(session.session_id):
            merged = merge_active_session(self._sessions.load(session.session_id), session)
            self._sessions.save(merged)
            return merged

    async def _close_tool_context(self, session: Session, request: RuntimeRequest) -> None:
        if self._tools is None:
            return
        try:
            await self._tools.close_context(self._tool_context(session, request))
        except Exception:
            return


def _tool_signature(intent: ToolIntent) -> str:
    arguments = json.dumps(
        mutable_json(intent.arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{intent.name}\n{arguments}"


def _resource_payload(resource: ToolResourceLink) -> dict[str, object]:
    return {
        "kind": resource.kind,
        "name": resource.name,
        "href": resource.href,
        "mime_type": resource.mime_type,
        "expires_at": resource.expires_at,
    }


def _skill_reference_payload(reference: SkillReference) -> dict[str, str]:
    return {
        "skill_id": reference.skill_id,
        "snapshot_id": reference.snapshot_id,
        "version": reference.version,
    }


def _usage_payload(usage: LLMUsage) -> dict[str, int | None]:
    payload: dict[str, int | None] = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    if usage.prompt_cache_hit_tokens is not None:
        payload["prompt_cache_hit_tokens"] = usage.prompt_cache_hit_tokens
        payload["prompt_cache_miss_tokens"] = usage.prompt_cache_miss_tokens
    return payload


def _compaction_payload(result: CompactionResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": result.status.value,
        "before_tokens": result.before_tokens,
        "after_tokens": result.after_tokens,
        "compacted_message_count": result.compacted_message_count,
        "recent_message_count": result.recent_message_count,
        "summary_preview": result.summary_preview,
        "target_met": result.target_met,
        "token_counter_mode": result.token_counter_mode,
    }
    if result.error_type is not None:
        payload["error_type"] = result.error_type
    if result.error_message is not None:
        payload["message"] = result.error_message
    if result.summary_prompt_tokens is not None:
        payload["summary_prompt_tokens"] = result.summary_prompt_tokens
    return payload


def _permission_message(decision: PermissionDecision) -> str:
    if decision is PermissionDecision.ALLOW:
        return "tool call allowed by permission policy"
    if decision is PermissionDecision.ASK:
        return "tool call requires approval; approval interaction is disabled"
    return "tool call denied by permission policy"


def _permission_result_code(decision: PermissionDecision) -> str:
    return "APPROVAL_REQUIRED" if decision is PermissionDecision.ASK else "PERMISSION_DENIED"


def _permission_result_message(decision: PermissionDecision) -> str:
    if decision is PermissionDecision.ASK:
        return "tool call requires approval; interactive approval is not enabled"
    return "tool call denied by permission policy"


def _approval_rejection_message(decision: ApprovalDecision) -> str:
    if decision.status is ApprovalDecisionStatus.TIMED_OUT:
        return "tool approval timed out"
    if decision.reason is None:
        return "tool approval was denied"
    return f"tool approval was denied: {decision.reason}"


def _error_code(error: SJTUClawError) -> str:
    if isinstance(error, SkillError):
        return error.code
    if isinstance(error, AgentLimitError) and str(error) == "MAX_STEPS_REACHED":
        return "MAX_STEPS_REACHED"
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", type(error).__name__).upper()


def _safe_error_message(error: SJTUClawError) -> str:
    return _safe_observation(str(error) or type(error).__name__)


def _safe_observation(value: str) -> str:
    cleaned = _AUTHORIZATION_VALUE.sub("Authorization=[redacted]", value)
    cleaned = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)
    cleaned = " ".join(cleaned.split()) or "runtime event"
    return cleaned[:300]
