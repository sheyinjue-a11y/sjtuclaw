"""Strict one-call LLM extraction of bounded automatic memory candidates."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sjtuclaw.models.llm import LLMRequest
from sjtuclaw.models.memory import MemoryCategory, MemoryEntry
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.ports.llm import LLMClient
from sjtuclaw.ports.memory import validate_memory_policy_rules
from sjtuclaw.runtime.memory_safety import ensure_safe_memory_content
from sjtuclaw.runtime.memory_time import normalize_memory_time

MAX_TURN_TEXT_LENGTH = 20_000
MAX_TOOL_RESULT_LENGTH = 20_000
MAX_EXISTING_MEMORIES = 8
MAX_EXISTING_CONTENT_LENGTH = 4_000
MAX_MEMORY_OPERATIONS = 8
MAX_OPERATION_CONTENT_LENGTH = 4_000
MAX_RESPONSE_LENGTH = 64_000
MAX_REQUEST_PAYLOAD_BYTES = 128_000
_PROMPT_FILE = "memory_extraction_prompt.md"
_TOP_LEVEL_FIELDS = frozenset({"operations"})
_ADD_FIELDS = frozenset(
    {
        "type",
        "content",
        "category",
        "importance",
        "originalTimeText",
        "eventAt",
        "eventTimezone",
    }
)
_UPDATE_FIELDS = _ADD_FIELDS | {"memoryId", "revision"}
_NOOP_FIELDS = frozenset({"type"})


class MemoryExtractionError(ValueError):
    """The model response did not satisfy the extraction protocol."""


class MemoryOperationType(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    NOOP = "NOOP"


@dataclass(frozen=True, slots=True)
class MemoryTurn:
    user_message: str
    assistant_answer: str
    tool_results: tuple[str, ...] = ()
    assistant_reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryOperation:
    operation_type: MemoryOperationType
    content: str | None = None
    category: MemoryCategory | None = None
    importance: int | None = None
    memory_id: str | None = None
    revision: int | None = None
    original_time_text: str | None = None
    event_at: datetime | None = None
    event_timezone: str | None = None


class _DuplicateJsonKeyError(ValueError):
    pass


class MemoryExtractor:
    def __init__(
        self,
        *,
        llm: LLMClient,
        clock: Callable[[], datetime] | None = None,
        local_timezone: ZoneInfo | None = None,
    ) -> None:
        self._llm = llm
        self._clock = clock or (lambda: datetime.now(UTC))
        self._local_timezone = local_timezone or _system_timezone()
        self._system_prompt = _load_system_prompt()

    async def extract(
        self,
        turn: MemoryTurn,
        *,
        rules: str,
        existing: tuple[MemoryEntry, ...],
    ) -> tuple[MemoryOperation, ...]:
        self._validate_input(turn, rules, existing)
        now = self._clock()
        payload = {
            "now": now.isoformat(),
            "timezone": self._local_timezone.key,
            "rules": rules,
            "user_message": turn.user_message,
            "assistant_answer": turn.assistant_answer,
            "tool_results": list(turn.tool_results),
            "existing": [_entry_payload(entry) for entry in existing],
        }
        compact_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(compact_payload.encode("utf-8")) > MAX_REQUEST_PAYLOAD_BYTES:
            raise ValueError("memory extraction input is too large")
        request = LLMRequest(
            messages=(
                Message(
                    "memory_extraction_system",
                    MessageRole.SYSTEM,
                    self._system_prompt,
                    now,
                    0,
                ),
                Message(
                    "memory_extraction_input",
                    MessageRole.USER,
                    compact_payload,
                    now,
                    1,
                ),
            )
        )
        response = await self._llm.complete(request)
        return self._parse_response(response.content, now=now)

    def _validate_input(
        self,
        turn: MemoryTurn,
        rules: str,
        existing: tuple[MemoryEntry, ...],
    ) -> None:
        validate_memory_policy_rules(rules)
        if not isinstance(turn, MemoryTurn):
            raise ValueError("turn is invalid")
        if (
            type(turn.user_message) is not str
            or not turn.user_message.strip()
            or len(turn.user_message) > MAX_TURN_TEXT_LENGTH
            or type(turn.assistant_answer) is not str
            or len(turn.assistant_answer) > MAX_TURN_TEXT_LENGTH
        ):
            raise ValueError("turn text is invalid")
        if (
            type(turn.tool_results) is not tuple
            or len(turn.tool_results) > MAX_EXISTING_MEMORIES
            or any(
                type(result) is not str or len(result) > MAX_TOOL_RESULT_LENGTH
                for result in turn.tool_results
            )
            or sum(len(result) for result in turn.tool_results) > MAX_TOOL_RESULT_LENGTH
        ):
            raise ValueError("turn tool results are invalid")
        if type(existing) is not tuple or len(existing) > MAX_EXISTING_MEMORIES:
            raise ValueError("existing memories are invalid")
        if any(
            not isinstance(entry, MemoryEntry)
            or len(entry.content) > MAX_EXISTING_CONTENT_LENGTH
            for entry in existing
        ):
            raise ValueError("existing memories are invalid")

    def _parse_response(
        self,
        content: str,
        *,
        now: datetime,
    ) -> tuple[MemoryOperation, ...]:
        try:
            if (
                type(content) is not str
                or not content.strip()
                or len(content) > MAX_RESPONSE_LENGTH
            ):
                raise ValueError
            document = json.loads(content, object_pairs_hook=_unique_object)
            if type(document) is not dict or frozenset(document) != _TOP_LEVEL_FIELDS:
                raise ValueError
            raw_operations = document["operations"]
            if (
                type(raw_operations) is not list
                or len(raw_operations) > MAX_MEMORY_OPERATIONS
            ):
                raise ValueError
            parsed = tuple(
                self._parse_operation(raw_operation, now=now)
                for raw_operation in raw_operations
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            raise MemoryExtractionError("invalid memory extraction response") from None

        safe: list[MemoryOperation] = []
        for operation in parsed:
            if operation.content is not None:
                try:
                    ensure_safe_memory_content(operation.content)
                except ValueError:
                    continue
            safe.append(operation)
        return tuple(safe)

    def _parse_operation(
        self,
        value: object,
        *,
        now: datetime,
    ) -> MemoryOperation:
        if type(value) is not dict:
            raise ValueError
        data = cast(dict[str, object], value)
        operation_type = MemoryOperationType(_required_string(data.get("type")))
        expected_fields = {
            MemoryOperationType.ADD: _ADD_FIELDS,
            MemoryOperationType.UPDATE: _UPDATE_FIELDS,
            MemoryOperationType.NOOP: _NOOP_FIELDS,
        }[operation_type]
        if frozenset(data) != expected_fields:
            raise ValueError
        if operation_type is MemoryOperationType.NOOP:
            return MemoryOperation(operation_type)

        content = _required_string(data["content"], allow_whitespace=False)
        if len(content) > MAX_OPERATION_CONTENT_LENGTH:
            raise ValueError
        category = MemoryCategory(_required_string(data["category"]))
        importance = data["importance"]
        if type(importance) is not int or not 1 <= importance <= 5:
            raise ValueError
        original_time_text = _optional_string(data["originalTimeText"])
        candidate_event_at = _optional_string(data["eventAt"])
        candidate_event_timezone = _optional_string(data["eventTimezone"])
        _validate_candidate_time(candidate_event_at, candidate_event_timezone)
        normalized_time = normalize_memory_time(
            original_time_text,
            now=now,
            timezone=self._local_timezone,
        )
        memory_id: str | None = None
        revision: int | None = None
        if operation_type is MemoryOperationType.UPDATE:
            memory_id = _required_string(data["memoryId"], allow_whitespace=False)
            revision_value = data["revision"]
            if type(revision_value) is not int or revision_value < 1:
                raise ValueError
            revision = revision_value
        return MemoryOperation(
            operation_type=operation_type,
            content=content,
            category=category,
            importance=importance,
            memory_id=memory_id,
            revision=revision,
            original_time_text=normalized_time.original_time_text,
            event_at=normalized_time.event_at,
            event_timezone=normalized_time.event_timezone,
        )


def _system_timezone() -> ZoneInfo:
    from tzlocal import get_localzone

    timezone = get_localzone()
    if not isinstance(timezone, ZoneInfo):
        return ZoneInfo(str(timezone))
    return timezone


def _load_system_prompt() -> str:
    prompt = (
        resources.files("sjtuclaw.prompts")
        .joinpath(_PROMPT_FILE)
        .read_text(encoding="utf-8")
        .strip()
    )
    if not prompt:
        raise ValueError("memory extraction prompt is unavailable")
    return prompt


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _required_string(value: object, *, allow_whitespace: bool = True) -> str:
    if type(value) is not str or not value or (not allow_whitespace and not value.strip()):
        raise ValueError
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _required_string(value, allow_whitespace=False)


def _validate_candidate_time(event_at: str | None, event_timezone: str | None) -> None:
    if (event_at is None) != (event_timezone is None):
        raise ValueError
    if event_at is None:
        return
    assert event_timezone is not None
    try:
        parsed = datetime.fromisoformat(event_at)
        ZoneInfo(event_timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError


def _entry_payload(entry: MemoryEntry) -> dict[str, object]:
    return {
        "memoryId": entry.memory_id,
        "content": entry.content,
        "category": entry.category.value,
        "importance": entry.importance,
        "originalTimeText": entry.original_time_text,
        "eventAt": None if entry.event_at is None else entry.event_at.isoformat(),
        "eventTimezone": entry.event_timezone,
        "revision": entry.revision,
    }
