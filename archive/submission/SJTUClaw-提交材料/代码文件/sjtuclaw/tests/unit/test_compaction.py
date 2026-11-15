from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from sjtuclaw.errors import ContextBudgetError, LLMError, StoreError
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.compaction import CompactionStatus
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMUsage
from sjtuclaw.models.memory import MemoryEntry
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.session import Session
from sjtuclaw.ports.llm import LLMClient
from sjtuclaw.runtime.compaction import CompactionService
from sjtuclaw.runtime.context_builder import ContextBuilder
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from tests.fakes import FakeClock, FakeLLMClient

CREATED_AT = datetime(2026, 7, 15, 8, tzinfo=UTC)
COMPACTED_AT = CREATED_AT + timedelta(hours=1)


@dataclass
class CharacterTokenCounter:
    requests: list[LLMRequest] = field(default_factory=list)

    def count(self, request: LLMRequest) -> int:
        self.requests.append(request)
        return sum(len(message.content) for message in request.messages)


class RecordingStore(InMemorySessionStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved: list[Session] = []

    def save(self, session: Session) -> None:
        self.saved.append(session)
        super().save(session)


class SaveFailingStore(InMemorySessionStore):
    def save(self, session: Session) -> None:
        raise StoreError("disk full\nAuthorization: secret")


def _message(
    sequence: int,
    role: MessageRole,
    content: str,
    *,
    tool_call_id: str | None = None,
) -> Message:
    return Message(
        message_id=f"message_{sequence:03d}",
        role=role,
        content=content,
        created_at=CREATED_AT,
        sequence_number=sequence,
        tool_call_id=tool_call_id,
    )


def _turns(count: int, *, content_size: int = 3) -> tuple[Message, ...]:
    messages: list[Message] = []
    sequence = 1
    for turn_number in range(1, count + 1):
        user_content = f"u{turn_number:02d}".ljust(content_size, "u")
        assistant_content = f"a{turn_number:02d}".ljust(content_size, "a")
        messages.append(_message(sequence, MessageRole.USER, user_content))
        sequence += 1
        messages.append(_message(sequence, MessageRole.ASSISTANT, assistant_content))
        sequence += 1
    return tuple(messages)


def _session(
    messages: tuple[Message, ...],
    *,
    summary: str = "",
    session_id: str = "session_001",
) -> Session:
    next_sequence = max((message.sequence_number for message in messages), default=0) + 1
    return Session(
        session_id=session_id,
        title="compaction test",
        messages=messages,
        summary=summary,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        next_sequence_number=next_sequence,
    )


def _response(content: str) -> LLMResponse:
    return LLMResponse(content, LLMUsage(10, 5, 15), "stop")


def _service(
    *,
    llm: LLMClient,
    sessions: InMemorySessionStore,
    context_builder: ContextBuilder | None = None,
    token_counter: CharacterTokenCounter | None = None,
    coordinator: SessionCoordinator | None = None,
    budget: int = 300,
    trigger_ratio: float = 0.8,
    target_ratio: float = 0.5,
    keep_recent_turns: int = 4,
    output_reserve: int = 10,
    trigger_tokens: int | None = None,
    target_tokens: int | None = None,
) -> CompactionService:
    return CompactionService(
        llm=llm,
        sessions=sessions,
        context_builder=context_builder or ContextBuilder("system"),
        token_counter=token_counter or CharacterTokenCounter(),
        coordinator=coordinator or SessionCoordinator(),
        clock=FakeClock(COMPACTED_AT),
        compact_prompt="COMPACT ONLY OLD SESSION TURNS",
        budget=budget,
        trigger_ratio=trigger_ratio,
        target_ratio=target_ratio,
        keep_recent_turns=keep_recent_turns,
        output_reserve=output_reserve,
        trigger_tokens=trigger_tokens,
        target_tokens=target_tokens,
    )


def test_projected_tokens_counts_full_context_and_output_reserve() -> None:
    sessions = InMemorySessionStore()
    session = _session(_turns(1), summary="old summary")
    counter = CharacterTokenCounter()
    service = _service(
        llm=FakeLLMClient([]),
        sessions=sessions,
        token_counter=counter,
        output_reserve=17,
    )

    projected = service.projected_tokens(session)

    assert projected == sum(
        len(message.content) for message in ContextBuilder("system").build(session)
    ) + 17
    assert [message.message_id for message in counter.requests[0].messages] == [
        "stable_system_prompt",
        "session_summary",
        "message_001",
        "message_002",
    ]


def test_below_trigger_returns_not_needed_without_llm_or_save() -> None:
    asyncio.run(_assert_below_trigger_returns_not_needed_without_llm_or_save())


async def _assert_below_trigger_returns_not_needed_without_llm_or_save() -> None:
    sessions = RecordingStore()
    session = _session(_turns(6))
    sessions.create(session)
    llm = FakeLLMClient([])
    coordinator = SessionCoordinator()
    service = _service(
        llm=llm,
        sessions=sessions,
        coordinator=coordinator,
        budget=200,
    )

    async with coordinator.execution_lock(session.session_id):
        unchanged, result = await service.maybe_compact_locked(session)

    assert unchanged is session
    assert result.status is CompactionStatus.NOT_NEEDED
    assert result.before_tokens == result.after_tokens
    assert result.compacted_message_count == 0
    assert llm.requests == []
    assert sessions.saved == []


def test_manual_force_compacts_and_preserves_session_sequence() -> None:
    asyncio.run(_assert_manual_force_compacts_and_preserves_session_sequence())


async def _assert_manual_force_compacts_and_preserves_session_sequence() -> None:
    sessions = RecordingStore()
    session = _session(_turns(6))
    sessions.create(session)
    llm = FakeLLMClient([_response("merged summary")])
    service = _service(llm=llm, sessions=sessions, budget=300)

    result = await service.compact_session(session.session_id, force=True)

    stored = sessions.load(session.session_id)
    assert result.status is CompactionStatus.COMPACTED
    assert result.compacted_message_count == 4
    assert result.recent_message_count == 8
    assert result.summary_preview == "merged summary"
    assert result.summary_prompt_tokens == 10
    assert result.target_met is True
    assert stored.summary == "merged summary"
    assert stored.messages == session.messages[4:]
    assert stored.next_sequence_number == session.next_sequence_number
    assert stored.updated_at == COMPACTED_AT
    assert stored.prompt_tokens == 10
    assert stored.completion_tokens == 5
    assert sessions.saved == [stored]


def test_absolute_trigger_uses_input_context_without_output_reserve() -> None:
    asyncio.run(_assert_absolute_trigger_uses_input_context_without_output_reserve())


async def _assert_absolute_trigger_uses_input_context_without_output_reserve() -> None:
    sessions = RecordingStore()
    session = _session(_turns(6))
    sessions.create(session)
    coordinator = SessionCoordinator()
    service = _service(
        llm=FakeLLMClient([]),
        sessions=sessions,
        coordinator=coordinator,
        budget=200,
        output_reserve=100,
        trigger_tokens=80,
        target_tokens=40,
    )

    async with coordinator.execution_lock(session.session_id):
        unchanged, result = await service.maybe_compact_locked(session)

    assert service.context_tokens(session) < 80
    assert service.projected_tokens(session) > 80
    assert unchanged is session
    assert result.status is CompactionStatus.NOT_NEEDED


def test_absolute_trigger_compacts_when_input_reaches_threshold() -> None:
    asyncio.run(_assert_absolute_trigger_compacts_when_input_reaches_threshold())


async def _assert_absolute_trigger_compacts_when_input_reaches_threshold() -> None:
    sessions = RecordingStore()
    session = _session(_turns(6))
    sessions.create(session)
    coordinator = SessionCoordinator()
    service = _service(
        llm=FakeLLMClient([_response("summary")]),
        sessions=sessions,
        coordinator=coordinator,
        budget=100,
        output_reserve=10,
        trigger_tokens=42,
        target_tokens=20,
    )

    async with coordinator.execution_lock(session.session_id):
        _, result = await service.maybe_compact_locked(session)

    assert result.status is CompactionStatus.COMPACTED


def test_compaction_keeps_complete_recent_turns_and_tool_exchange() -> None:
    asyncio.run(_assert_compaction_keeps_complete_recent_turns_and_tool_exchange())


async def _assert_compaction_keeps_complete_recent_turns_and_tool_exchange() -> None:
    old_tool_turn = (
        _message(1, MessageRole.USER, "calculate old result"),
        _message(2, MessageRole.ASSISTANT, "calling calculator", tool_call_id="call_1"),
        _message(3, MessageRole.TOOL, "42", tool_call_id="call_1"),
        _message(4, MessageRole.ASSISTANT, "the result is 42"),
    )
    second_old_turn = (
        _message(5, MessageRole.USER, "second old question"),
        _message(6, MessageRole.ASSISTANT, "second old answer"),
    )
    recent = tuple(
        _message(message.sequence_number + 6, message.role, message.content)
        for message in _turns(4)
    )
    session = _session((*old_tool_turn, *second_old_turn, *recent))
    sessions = InMemorySessionStore()
    sessions.create(session)
    llm = FakeLLMClient([_response("tool result retained in summary")])
    service = _service(llm=llm, sessions=sessions, budget=500)

    result = await service.compact_session(session.session_id)

    assert result.compacted_message_count == 6
    assert sessions.load(session.session_id).messages == recent
    payload = json.loads(llm.requests[0].messages[1].content)
    assert payload["old_messages"] == [
        {
            "content": message.content,
            "role": message.role.value,
            "sequence": message.sequence_number,
            "tool_call_id": message.tool_call_id,
            "tool_calls": [],
            "tool_name": message.tool_name,
            "tool_result_status": (
                None
                if message.tool_result_status is None
                else message.tool_result_status.value
            ),
            "tool_error_code": message.tool_error_code,
        }
        for message in (*old_tool_turn, *second_old_turn)
    ]


def test_compaction_keeps_four_completed_turns_plus_active_user_turn() -> None:
    asyncio.run(_assert_compaction_keeps_completed_turns_plus_active_user_turn())


async def _assert_compaction_keeps_completed_turns_plus_active_user_turn() -> None:
    messages = (*_turns(5), _message(11, MessageRole.USER, "active question"))
    session = _session(messages)
    sessions = InMemorySessionStore()
    sessions.create(session)
    service = _service(
        llm=FakeLLMClient([_response("first turn compacted")]),
        sessions=sessions,
        budget=500,
    )

    result = await service.compact_session(session.session_id)

    assert result.compacted_message_count == 2
    assert sessions.load(session.session_id).messages == messages[2:]


def test_summary_request_contains_existing_summary_but_no_stable_context() -> None:
    asyncio.run(_assert_summary_request_contains_existing_summary_but_no_stable_context())


async def _assert_summary_request_contains_existing_summary_but_no_stable_context() -> None:
    class StaticRetriever:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def retrieve(self, query: str) -> tuple[MemoryEntry, ...]:
            self.queries.append(query)
            return (MemoryEntry("memory_001", "MEMORY_SECRET", CREATED_AT),)

    session = _session(_turns(6), summary="existing task state")
    sessions = InMemorySessionStore()
    sessions.create(session)
    llm = FakeLLMClient([_response("updated task state")])
    retriever = StaticRetriever()
    builder = ContextBuilder(
        "SYSTEM_SECRET TOOL_DEFINITION_SECRET",
        soul="SOUL_SECRET",
        memory_retriever=retriever,
    )
    service = _service(
        llm=llm,
        sessions=sessions,
        context_builder=builder,
        budget=1000,
    )

    await service.compact_session(session.session_id)

    request = llm.requests[0]
    assert len(request.messages) == 2
    assert request.messages[0].role is MessageRole.SYSTEM
    assert request.messages[0].content == "COMPACT ONLY OLD SESSION TURNS"
    payload = json.loads(request.messages[1].content)
    assert payload["existing_summary"] == "existing task state"
    compact_input = "\n".join(message.content for message in request.messages)
    assert "SYSTEM_SECRET" not in compact_input
    assert "TOOL_DEFINITION_SECRET" not in compact_input
    assert "SOUL_SECRET" not in compact_input
    assert "MEMORY_SECRET" not in compact_input
    assert retriever.queries == ["u06", "", "u06"]


@pytest.mark.parametrize("invalid_summary", ["", "  \n\t  ", "bad\x00summary"])
def test_empty_or_invalid_summary_keeps_original_history(invalid_summary: str) -> None:
    asyncio.run(_assert_empty_or_invalid_summary_keeps_original_history(invalid_summary))


async def _assert_empty_or_invalid_summary_keeps_original_history(
    invalid_summary: str,
) -> None:
    sessions = RecordingStore()
    session = _session(_turns(6), summary="original summary")
    sessions.create(session)
    service = _service(
        llm=FakeLLMClient([_response(invalid_summary)]),
        sessions=sessions,
        budget=500,
    )

    result = await service.compact_session(session.session_id)

    assert result.status is CompactionStatus.FAILED
    assert result.error_type == "InvalidSummary"
    assert result.summary_prompt_tokens == 10
    stored = sessions.load(session.session_id)
    assert stored.messages == session.messages
    assert stored.summary == session.summary
    assert stored.prompt_tokens == 10
    assert stored.completion_tokens == 5
    assert sessions.saved == [stored]


def test_expected_llm_failure_is_cleaned_and_keeps_original_history() -> None:
    asyncio.run(_assert_expected_llm_failure_is_cleaned_and_keeps_original_history())


async def _assert_expected_llm_failure_is_cleaned_and_keeps_original_history() -> None:
    class FailingLLM:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise LLMError("provider failed\nAuthorization: Bearer TOP_SECRET")

    sessions = RecordingStore()
    session = _session(_turns(6))
    sessions.create(session)
    service = _service(llm=FailingLLM(), sessions=sessions, budget=500)

    result = await service.compact_session(session.session_id)

    assert result.status is CompactionStatus.FAILED
    assert result.error_type == "LLMError"
    assert "\n" not in (result.error_message or "")
    assert "TOP_SECRET" not in (result.error_message or "")
    assert len(result.error_message or "") <= 300
    assert sessions.load(session.session_id) == session
    assert sessions.saved == []


def test_save_failure_returns_failed_without_replacing_stored_history() -> None:
    asyncio.run(_assert_save_failure_returns_failed_without_replacing_stored_history())


async def _assert_save_failure_returns_failed_without_replacing_stored_history() -> None:
    sessions = SaveFailingStore()
    session = _session(_turns(6))
    sessions.create(session)
    service = _service(
        llm=FakeLLMClient([_response("valid summary")]),
        sessions=sessions,
        budget=500,
    )

    result = await service.compact_session(session.session_id)

    assert result.status is CompactionStatus.FAILED
    assert result.error_type == "StoreError"
    assert "\n" not in (result.error_message or "")
    assert sessions.load(session.session_id) == session


def test_programming_error_from_llm_is_not_swallowed() -> None:
    asyncio.run(_assert_programming_error_from_llm_is_not_swallowed())


async def _assert_programming_error_from_llm_is_not_swallowed() -> None:
    class BrokenFakeLLM:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("test double misconfigured")

    sessions = InMemorySessionStore()
    session = _session(_turns(6))
    sessions.create(session)
    service = _service(llm=BrokenFakeLLM(), sessions=sessions, budget=500)

    with pytest.raises(AssertionError, match="misconfigured"):
        await service.compact_session(session.session_id)

    assert sessions.load(session.session_id) == session


def test_force_with_no_old_turn_returns_no_eligible_without_save() -> None:
    asyncio.run(_assert_force_with_no_old_turn_returns_no_eligible_without_save())


async def _assert_force_with_no_old_turn_returns_no_eligible_without_save() -> None:
    sessions = RecordingStore()
    session = _session(_turns(4))
    sessions.create(session)
    llm = FakeLLMClient([])
    service = _service(llm=llm, sessions=sessions, budget=300)

    result = await service.compact_session(session.session_id, force=True)

    assert result.status is CompactionStatus.NO_ELIGIBLE_MESSAGES
    assert result.recent_message_count == len(session.messages)
    assert result.compacted_message_count == 0
    assert llm.requests == []
    assert sessions.saved == []


def test_no_eligible_turn_over_hard_budget_raises_without_save() -> None:
    asyncio.run(_assert_no_eligible_turn_over_hard_budget_raises_without_save())


async def _assert_no_eligible_turn_over_hard_budget_raises_without_save() -> None:
    sessions = RecordingStore()
    session = _session(_turns(4, content_size=30))
    sessions.create(session)
    service = _service(llm=FakeLLMClient([]), sessions=sessions, budget=100)

    with pytest.raises(ContextBudgetError):
        await service.compact_session(session.session_id)

    assert sessions.load(session.session_id) == session
    assert sessions.saved == []


def test_stable_context_over_target_raises_before_llm() -> None:
    asyncio.run(_assert_stable_context_over_target_raises_before_llm())


async def _assert_stable_context_over_target_raises_before_llm() -> None:
    sessions = RecordingStore()
    session = _session(_turns(6))
    sessions.create(session)
    llm = FakeLLMClient([])
    service = _service(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("S" * 100),
        budget=50,
    )

    with pytest.raises(ContextBudgetError, match="stable context"):
        await service.compact_session(session.session_id, force=True)

    assert llm.requests == []
    assert sessions.load(session.session_id) == session
    assert sessions.saved == []


def test_stable_context_over_target_but_under_hard_budget_raises() -> None:
    asyncio.run(_assert_stable_context_over_target_but_under_hard_budget_raises())


async def _assert_stable_context_over_target_but_under_hard_budget_raises() -> None:
    sessions = RecordingStore()
    session = _session(_turns(6))
    sessions.create(session)
    llm = FakeLLMClient([])
    service = _service(
        llm=llm,
        sessions=sessions,
        context_builder=ContextBuilder("S" * 50),
        budget=100,
        target_ratio=0.5,
        output_reserve=10,
    )

    with pytest.raises(ContextBudgetError, match="compaction target"):
        await service.compact_session(session.session_id, force=True)

    assert llm.requests == []
    assert sessions.load(session.session_id) == session
    assert sessions.saved == []


def test_automatic_path_checks_stable_target_before_trigger_return() -> None:
    asyncio.run(_assert_automatic_path_checks_stable_target_before_trigger_return())


async def _assert_automatic_path_checks_stable_target_before_trigger_return() -> None:
    sessions = RecordingStore()
    session = _session(_turns(1))
    sessions.create(session)
    coordinator = SessionCoordinator()
    service = _service(
        llm=FakeLLMClient([]),
        sessions=sessions,
        context_builder=ContextBuilder("S" * 50),
        coordinator=coordinator,
        budget=100,
        trigger_ratio=0.8,
        target_ratio=0.5,
        output_reserve=10,
    )

    async with coordinator.execution_lock(session.session_id):
        with pytest.raises(ContextBudgetError, match="compaction target"):
            await service.maybe_compact_locked(session)

    assert sessions.saved == []


def test_compacted_candidate_over_hard_budget_raises_without_save() -> None:
    asyncio.run(_assert_compacted_candidate_over_hard_budget_raises_without_save())


async def _assert_compacted_candidate_over_hard_budget_raises_without_save() -> None:
    sessions = RecordingStore()
    session = _session(_turns(6, content_size=20))
    sessions.create(session)
    service = _service(
        llm=FakeLLMClient([_response("summary")]),
        sessions=sessions,
        budget=100,
    )

    with pytest.raises(ContextBudgetError, match="after compaction"):
        await service.compact_session(session.session_id, force=True)

    assert sessions.load(session.session_id) == session
    assert sessions.saved == []


def test_below_target_false_still_saves_when_under_hard_budget() -> None:
    asyncio.run(_assert_below_target_false_still_saves_when_under_hard_budget())


async def _assert_below_target_false_still_saves_when_under_hard_budget() -> None:
    sessions = RecordingStore()
    session = _session(_turns(8, content_size=10))
    sessions.create(session)
    service = _service(
        llm=FakeLLMClient([_response("S")]),
        sessions=sessions,
        budget=200,
        trigger_ratio=0.8,
        target_ratio=0.5,
    )

    result = await service.compact_session(session.session_id, force=False)

    assert result.status is CompactionStatus.COMPACTED
    assert result.after_tokens <= 200
    assert result.after_tokens > 100
    assert result.target_met is False
    assert sessions.saved == [sessions.load(session.session_id)]


def test_summary_preview_has_fixed_reasonable_limit() -> None:
    asyncio.run(_assert_summary_preview_has_fixed_reasonable_limit())


async def _assert_summary_preview_has_fixed_reasonable_limit() -> None:
    sessions = InMemorySessionStore()
    session = _session(_turns(6))
    sessions.create(session)
    service = _service(
        llm=FakeLLMClient([_response("x" * 300)]),
        sessions=sessions,
        budget=1000,
    )

    result = await service.compact_session(session.session_id)

    assert result.status is CompactionStatus.COMPACTED
    assert len(result.summary_preview) <= 240
    assert result.summary_preview.endswith("...")


def test_compact_session_waits_for_the_shared_execution_lock() -> None:
    asyncio.run(_assert_compact_session_waits_for_the_shared_execution_lock())


async def _assert_compact_session_waits_for_the_shared_execution_lock() -> None:
    sessions = InMemorySessionStore()
    session = _session(_turns(6))
    sessions.create(session)
    coordinator = SessionCoordinator()
    service = _service(
        llm=FakeLLMClient([_response("summary")]),
        sessions=sessions,
        coordinator=coordinator,
        budget=500,
    )

    lock = coordinator.execution_lock(session.session_id)
    async with lock:
        task = asyncio.create_task(service.compact_session(session.session_id))
        await asyncio.sleep(0)
        assert task.done() is False

    result = await asyncio.wait_for(task, timeout=0.5)
    assert result.status is CompactionStatus.COMPACTED
