"""Session compaction with full-context budgeting and loss-safe persistence."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import replace

from sjtuclaw.errors import ContextBudgetError, SJTUClawError, StoreError
from sjtuclaw.models.compaction import CompactionResult, CompactionStatus
from sjtuclaw.models.llm import LLMRequest, LLMToolMode
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.session import Session
from sjtuclaw.models.tool import ToolDefinition
from sjtuclaw.ports.llm import LLMClient
from sjtuclaw.ports.runtime import Clock
from sjtuclaw.ports.stores import SessionStore
from sjtuclaw.ports.token_counter import TokenCounter
from sjtuclaw.runtime.context_builder import ContextBuilder
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_state import merge_active_session
from sjtuclaw.runtime.token_usage import accumulate_token_usage

_SUMMARY_PREVIEW_LIMIT = 240
_ERROR_MESSAGE_LIMIT = 300
_AUTHORIZATION_VALUE = re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[^\r\n,;]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|secret)\b\s*[:=]\s*\S+"
)


class CompactionService:
    """Compact old complete agent turns while preserving immutable context."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        sessions: SessionStore,
        context_builder: ContextBuilder,
        token_counter: TokenCounter,
        coordinator: SessionCoordinator,
        clock: Clock,
        compact_prompt: str,
        budget: int,
        trigger_ratio: float,
        target_ratio: float,
        keep_recent_turns: int,
        output_reserve: int,
        trigger_tokens: int | None = None,
        target_tokens: int | None = None,
        token_counter_mode: str | Callable[[], str] = "deterministic",
        tools: tuple[ToolDefinition, ...] = (),
        tool_mode: LLMToolMode | None = None,
    ) -> None:
        normalized_prompt = compact_prompt.strip()
        if not normalized_prompt:
            raise ValueError("compact_prompt must not be empty")
        if budget < 1:
            raise ValueError("budget must be positive")
        if not 0 < target_ratio < trigger_ratio < 1:
            raise ValueError("ratios must satisfy 0 < target_ratio < trigger_ratio < 1")
        if keep_recent_turns < 1:
            raise ValueError("keep_recent_turns must be positive")
        if not 0 <= output_reserve < budget:
            raise ValueError("output_reserve must be non-negative and smaller than budget")
        if (trigger_tokens is None) != (target_tokens is None):
            raise ValueError("absolute compaction thresholds must be supplied together")
        if trigger_tokens is not None:
            assert target_tokens is not None
            if not 0 < target_tokens < trigger_tokens < budget:
                raise ValueError(
                    "absolute thresholds must satisfy 0 < target < trigger < budget"
                )
        if isinstance(token_counter_mode, str) and not token_counter_mode.strip():
            raise ValueError("token_counter_mode must not be empty")

        self._llm = llm
        self._sessions = sessions
        self._context_builder = context_builder
        self._token_counter = token_counter
        self._coordinator = coordinator
        self._clock = clock
        self._compact_prompt = normalized_prompt
        self._budget = budget
        self._trigger_ratio = trigger_ratio
        self._target_ratio = target_ratio
        self._keep_recent_turns = keep_recent_turns
        self._output_reserve = output_reserve
        self._trigger_tokens = trigger_tokens
        self._target_tokens = target_tokens
        self._token_counter_mode = (
            token_counter_mode.strip()
            if isinstance(token_counter_mode, str)
            else token_counter_mode
        )
        self._tools = tools
        self._tool_mode = tool_mode
        if bool(tools) != (tool_mode is not None):
            raise ValueError("compaction tools and tool_mode must be supplied together")

    def projected_tokens(self, session: Session) -> int:
        """Count the complete stable/session context plus reserved model output."""

        return self.context_tokens(session) + self._output_reserve

    def context_tokens(self, session: Session) -> int:
        """Count the actual prompt context without the output reserve."""

        request = LLMRequest(
            self._context_builder.build(session),
            self._tools,
            self._tool_mode,
        )
        return self._token_counter.count(request)

    @property
    def context_window_tokens(self) -> int:
        return self._budget

    @property
    def output_reserve_tokens(self) -> int:
        return self._output_reserve

    @property
    def compaction_trigger_tokens(self) -> int:
        return self._trigger_tokens or round(self._budget * self._trigger_ratio)

    @property
    def compaction_target_tokens(self) -> int:
        return self._target_tokens or round(self._budget * self._target_ratio)

    async def maybe_compact_locked(
        self,
        session: Session,
    ) -> tuple[Session, CompactionResult]:
        """Run and persist automatic compaction; caller owns the execution lock."""

        return await self._compact_and_save_locked(session, force=False)

    async def compact_session(
        self,
        session_id: str,
        force: bool = True,
    ) -> CompactionResult:
        """Load, compact, and save a session under its shared execution lock."""

        async with self._coordinator.execution_lock(session_id):
            try:
                session = self._sessions.load(session_id)
            except StoreError as exc:
                return self._failure_without_session(session_id, exc)

            _, result = await self._compact_and_save_locked(session, force=force)
            return result

    async def _compact_and_save_locked(
        self,
        session: Session,
        *,
        force: bool,
    ) -> tuple[Session, CompactionResult]:
        compacted, result = await self._compact_locked(session, force=force)
        usage_changed = (
            compacted.prompt_tokens != session.prompt_tokens
            or compacted.completion_tokens != session.completion_tokens
        )
        if result.status is CompactionStatus.COMPACTED or usage_changed:
            try:
                async with self._coordinator.admission_lock(session.session_id):
                    current = self._sessions.load(session.session_id)
                    compacted = merge_active_session(current, compacted)
                    self._sessions.save(compacted)
            except StoreError as exc:
                result = self._failure_result(
                    session,
                    result.before_tokens,
                    exc,
                    summary_prompt_tokens=result.summary_prompt_tokens,
                )
                compacted = session

        if result.status is CompactionStatus.FAILED and result.before_tokens > self._budget:
            raise ContextBudgetError(
                f"session {session.session_id} exceeds hard budget and compaction failed"
            )
        return compacted, result

    async def _compact_locked(
        self,
        session: Session,
        *,
        force: bool,
    ) -> tuple[Session, CompactionResult]:
        context_before_tokens = self.context_tokens(session)
        before_tokens = context_before_tokens + self._output_reserve
        stable_only = replace(session, summary="", messages=())
        stable_context_tokens = self.context_tokens(stable_only)
        target_tokens = (
            self._target_tokens
            if self._target_tokens is not None
            else self._budget * self._target_ratio - self._output_reserve
        )
        if stable_context_tokens > target_tokens:
            raise ContextBudgetError(
                "stable context and output reserve exceed compaction target for "
                f"{session.session_id}"
            )
        trigger_tokens = (
            self._trigger_tokens
            if self._trigger_tokens is not None
            else self._budget * self._trigger_ratio - self._output_reserve
        )
        if not force and context_before_tokens < trigger_tokens:
            return session, self._unchanged_result(
                session,
                CompactionStatus.NOT_NEEDED,
                before_tokens,
            )

        old_messages, recent_messages = self._split_complete_turns(session.messages)
        if not old_messages:
            if before_tokens > self._budget:
                raise ContextBudgetError(
                    f"session {session.session_id} exceeds hard budget but has no eligible turns"
                )
            return session, self._unchanged_result(
                session,
                CompactionStatus.NO_ELIGIBLE_MESSAGES,
                before_tokens,
            )

        request = self._summary_request(session, old_messages)
        try:
            response = await self._llm.complete(request)
        except ContextBudgetError:
            raise
        except SJTUClawError as exc:
            return session, self._failure_result(session, before_tokens, exc)

        session = accumulate_token_usage(session, response.usage, self._clock.now())

        summary, validation_error = self._validated_summary(response.content)
        if validation_error is not None:
            return session, self._failure_result(
                session,
                before_tokens,
                validation_error,
                error_type="InvalidSummary",
                summary_prompt_tokens=response.usage.prompt_tokens,
            )
        assert summary is not None

        compacted = replace(
            session,
            summary=summary,
            messages=recent_messages,
            updated_at=self._clock.now(),
        )
        after_tokens = self.projected_tokens(compacted)
        if after_tokens > self._budget:
            raise ContextBudgetError(
                f"session {session.session_id} still exceeds hard budget after compaction"
            )

        result = CompactionResult(
            status=CompactionStatus.COMPACTED,
            session_id=session.session_id,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            compacted_message_count=len(old_messages),
            recent_message_count=len(recent_messages),
            summary_preview=self._summary_preview(summary),
            target_met=(
                self.context_tokens(compacted) <= self.compaction_target_tokens
                if self._target_tokens is not None
                else after_tokens <= self._budget * self._target_ratio
            ),
            token_counter_mode=self._current_token_counter_mode(),
            summary_prompt_tokens=response.usage.prompt_tokens,
        )
        return compacted, result

    def _split_complete_turns(
        self,
        messages: tuple[Message, ...],
    ) -> tuple[tuple[Message, ...], tuple[Message, ...]]:
        user_starts = [
            index for index, message in enumerate(messages) if message.role is MessageRole.USER
        ]
        if not user_starts:
            return (), messages
        completed_starts = user_starts
        if (
            messages[-1].role is not MessageRole.ASSISTANT
            or messages[-1].tool_calls
        ):
            completed_starts = user_starts[:-1]
        if len(completed_starts) <= self._keep_recent_turns:
            return (), messages
        recent_start = completed_starts[-self._keep_recent_turns]
        return messages[:recent_start], messages[recent_start:]

    def _summary_request(
        self,
        session: Session,
        old_messages: tuple[Message, ...],
    ) -> LLMRequest:
        payload = {
            "existing_summary": session.summary,
            "old_messages": [
                {
                    "sequence": message.sequence_number,
                    "role": message.role.value,
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                    "tool_calls": [
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "arguments_json": call.arguments_json,
                        }
                        for call in message.tool_calls
                    ],
                    "tool_name": message.tool_name,
                    "tool_result_status": (
                        None
                        if message.tool_result_status is None
                        else message.tool_result_status.value
                    ),
                    "tool_error_code": message.tool_error_code,
                }
                for message in old_messages
            ],
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt_message = Message(
            message_id="compaction_prompt",
            role=MessageRole.SYSTEM,
            content=self._compact_prompt,
            created_at=session.created_at,
            sequence_number=0,
        )
        payload_message = Message(
            message_id="compaction_payload",
            role=MessageRole.USER,
            content=serialized,
            created_at=session.created_at,
            sequence_number=0,
        )
        return LLMRequest((prompt_message, payload_message))

    @staticmethod
    def _validated_summary(content: str) -> tuple[str | None, str | None]:
        summary = content.strip()
        if not summary:
            return None, "compaction returned an empty summary"
        if any(
            unicodedata.category(character) == "Cc" and character not in "\n\r\t"
            for character in summary
        ):
            return None, "compaction returned a summary with invalid control characters"
        return summary, None

    def _unchanged_result(
        self,
        session: Session,
        status: CompactionStatus,
        tokens: int,
    ) -> CompactionResult:
        return CompactionResult(
            status=status,
            session_id=session.session_id,
            before_tokens=tokens,
            after_tokens=tokens,
            compacted_message_count=0,
            recent_message_count=len(session.messages),
            summary_preview=self._summary_preview(session.summary),
            target_met=(
                tokens - self._output_reserve <= self.compaction_target_tokens
                if self._target_tokens is not None
                else tokens <= self._budget * self._target_ratio
            ),
            token_counter_mode=self._current_token_counter_mode(),
        )

    def _failure_result(
        self,
        session: Session,
        before_tokens: int,
        error: SJTUClawError | str,
        *,
        error_type: str | None = None,
        summary_prompt_tokens: int | None = None,
    ) -> CompactionResult:
        resolved_type = error_type or type(error).__name__
        return CompactionResult(
            status=CompactionStatus.FAILED,
            session_id=session.session_id,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
            compacted_message_count=0,
            recent_message_count=len(session.messages),
            summary_preview=self._summary_preview(session.summary),
            target_met=False,
            token_counter_mode=self._current_token_counter_mode(),
            summary_prompt_tokens=summary_prompt_tokens,
            error_type=resolved_type,
            error_message=self._clean_error_message(str(error)),
        )

    def _failure_without_session(
        self,
        session_id: str,
        error: SJTUClawError,
    ) -> CompactionResult:
        return CompactionResult(
            status=CompactionStatus.FAILED,
            session_id=session_id,
            before_tokens=0,
            after_tokens=0,
            compacted_message_count=0,
            recent_message_count=0,
            summary_preview="",
            target_met=False,
            token_counter_mode=self._current_token_counter_mode(),
            error_type=type(error).__name__,
            error_message=self._clean_error_message(str(error)),
        )

    def _current_token_counter_mode(self) -> str:
        mode = (
            self._token_counter_mode()
            if callable(self._token_counter_mode)
            else self._token_counter_mode
        )
        if not mode.strip():
            raise ValueError("token_counter_mode must not be empty")
        return str(mode)

    @staticmethod
    def _summary_preview(summary: str) -> str:
        preview = " ".join(summary.split())
        if len(preview) <= _SUMMARY_PREVIEW_LIMIT:
            return preview
        return f"{preview[: _SUMMARY_PREVIEW_LIMIT - 3].rstrip()}..."

    @staticmethod
    def _clean_error_message(message: str) -> str:
        cleaned = _AUTHORIZATION_VALUE.sub("Authorization: [redacted]", message)
        cleaned = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}: [redacted]", cleaned)
        cleaned = " ".join(cleaned.split()) or "compaction failed"
        if len(cleaned) <= _ERROR_MESSAGE_LIMIT:
            return cleaned
        return f"{cleaned[: _ERROR_MESSAGE_LIMIT - 3].rstrip()}..."
