from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sjtuclaw.models.llm import LLMResponse, LLMUsage
from sjtuclaw.models.memory import MemoryCategory, MemoryEntry
from sjtuclaw.models.message import MessageRole
from sjtuclaw.runtime.memory_extractor import (
    MAX_EXISTING_MEMORIES,
    MAX_MEMORY_OPERATIONS,
    MAX_TURN_TEXT_LENGTH,
    MemoryExtractionError,
    MemoryExtractor,
    MemoryOperationType,
    MemoryTurn,
)
from tests.fakes import FakeLLMClient

NOW = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
SHANGHAI = ZoneInfo("Asia/Shanghai")
USAGE = LLMUsage(10, 5, 15)


def _response(document: object) -> LLMResponse:
    content = document if isinstance(document, str) else json.dumps(document)
    return LLMResponse(content, USAGE, "stop")


def _extractor(llm: FakeLLMClient) -> MemoryExtractor:
    return MemoryExtractor(
        llm=llm,
        clock=lambda: NOW,
        local_timezone=SHANGHAI,
    )


def _turn() -> MemoryTurn:
    return MemoryTurn(
        user_message="明天下午三点提醒我提交报告",
        assistant_answer="好的，我会记住你的报告安排。",
        tool_results=("calendar lookup: no conflict",),
        assistant_reasoning="PRIVATE_CHAIN_OF_THOUGHT",
    )


def _existing() -> MemoryEntry:
    return MemoryEntry(
        "memory_existing",
        "旧报告时间",
        NOW,
        category=MemoryCategory.EVENT,
        revision=2,
    )


def test_extract_accepts_add_update_noop_and_sends_exactly_two_messages_once() -> None:
    async def scenario() -> None:
        llm = FakeLLMClient(
            [
                _response(
                    {
                        "operations": [
                            {
                                "type": "ADD",
                                "content": "用户明天下午三点提交报告",
                                "category": "event",
                                "importance": 4,
                                "originalTimeText": "明天下午3点",
                                "eventAt": "2099-01-01T00:00:00Z",
                                "eventTimezone": "Etc/UTC",
                            },
                            {
                                "type": "UPDATE",
                                "memoryId": "memory_existing",
                                "revision": 2,
                                "content": "报告时间改为明天下午三点",
                                "category": "event",
                                "importance": 5,
                                "originalTimeText": None,
                                "eventAt": None,
                                "eventTimezone": None,
                            },
                            {"type": "NOOP"},
                        ]
                    }
                )
            ]
        )

        operations = await _extractor(llm).extract(
            _turn(),
            rules="只保存长期有用的信息。",
            existing=(_existing(),),
        )

        assert [operation.operation_type for operation in operations] == [
            MemoryOperationType.ADD,
            MemoryOperationType.UPDATE,
            MemoryOperationType.NOOP,
        ]
        assert operations[0].event_at == datetime(2026, 7, 29, 7, 0, tzinfo=UTC)
        assert operations[0].event_timezone == "Asia/Shanghai"
        assert operations[1].memory_id == "memory_existing"
        assert operations[1].revision == 2
        assert len(llm.requests) == 1
        request = llm.requests[0]
        assert request.tools == ()
        assert len(request.messages) == 2
        assert [message.role for message in request.messages] == [
            MessageRole.SYSTEM,
            MessageRole.USER,
        ]
        payload = json.loads(request.messages[1].content)
        assert payload == {
            "now": NOW.isoformat(),
            "timezone": "Asia/Shanghai",
            "rules": "只保存长期有用的信息。",
            "user_message": "明天下午三点提醒我提交报告",
            "assistant_answer": "好的，我会记住你的报告安排。",
            "tool_results": ["calendar lookup: no conflict"],
            "existing": [
                {
                    "memoryId": "memory_existing",
                    "content": "旧报告时间",
                    "category": "event",
                    "importance": 3,
                    "originalTimeText": None,
                    "eventAt": None,
                    "eventTimezone": None,
                    "revision": 2,
                }
            ],
        }
        assert "PRIVATE_CHAIN_OF_THOUGHT" not in request.messages[1].content
        assert request.messages[1].content == json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "content",
    [
        "",
        "```json\n{\"operations\": []}\n```",
        {"operations": [], "extra": True},
        {"operations": [{"type": "DELETE"}]},
        {"operations": [{"type": "BOGUS"}]},
        {
            "operations": [
                {"type": "NOOP"},
                {
                    "type": "UPDATE",
                    "memoryId": "memory_existing",
                    "revision": 0,
                    "content": "bad",
                    "category": "event",
                    "importance": 3,
                    "originalTimeText": None,
                    "eventAt": None,
                    "eventTimezone": None,
                },
            ]
        },
    ],
)
def test_extract_rejects_malformed_or_partially_invalid_batches(content: object) -> None:
    async def scenario() -> None:
        llm = FakeLLMClient([_response(content)])

        with pytest.raises(MemoryExtractionError, match="invalid memory extraction response"):
            await _extractor(llm).extract(
                _turn(),
                rules="只保存长期信息。",
                existing=(_existing(),),
            )

        assert len(llm.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("revision", [True, "2", -1])
def test_extract_rejects_non_positive_integer_revision(revision: object) -> None:
    async def scenario() -> None:
        llm = FakeLLMClient(
            [
                _response(
                    {
                        "operations": [
                            {
                                "type": "UPDATE",
                                "memoryId": "memory_existing",
                                "revision": revision,
                                "content": "更新",
                                "category": "event",
                                "importance": 3,
                                "originalTimeText": None,
                                "eventAt": None,
                                "eventTimezone": None,
                            }
                        ]
                    }
                )
            ]
        )

        with pytest.raises(MemoryExtractionError):
            await _extractor(llm).extract(
                _turn(),
                rules="只保存长期信息。",
                existing=(_existing(),),
            )

    asyncio.run(scenario())


def test_extract_rejects_llm_provenance_fields() -> None:
    async def scenario() -> None:
        llm = FakeLLMClient(
            [
                _response(
                    {
                        "operations": [
                            {
                                "type": "ADD",
                                "content": "安全内容",
                                "category": "other",
                                "importance": 3,
                                "originalTimeText": None,
                                "eventAt": None,
                                "eventTimezone": None,
                                "sourceKind": "manual",
                            }
                        ]
                    }
                )
            ]
        )

        with pytest.raises(MemoryExtractionError):
            await _extractor(llm).extract(_turn(), rules="保存信息。", existing=())

    asyncio.run(scenario())


def test_extract_discards_secret_candidates_individually_without_echoing_secret() -> None:
    async def scenario() -> None:
        secret = "sk-this-must-never-appear"
        llm = FakeLLMClient(
            [
                _response(
                    {
                        "operations": [
                            {
                                "type": "ADD",
                                "content": f"API key: {secret}",
                                "category": "other",
                                "importance": 3,
                                "originalTimeText": None,
                                "eventAt": None,
                                "eventTimezone": None,
                            },
                            {
                                "type": "ADD",
                                "content": "用户偏好中文回复",
                                "category": "preference",
                                "importance": 5,
                                "originalTimeText": None,
                                "eventAt": None,
                                "eventTimezone": None,
                            },
                        ]
                    }
                )
            ]
        )

        operations = await _extractor(llm).extract(
            _turn(),
            rules="保存长期偏好。",
            existing=(),
        )

        assert len(operations) == 1
        assert operations[0].content == "用户偏好中文回复"
        assert secret not in repr(operations)

    asyncio.run(scenario())


def test_extract_does_not_trust_model_time_for_ambiguous_text() -> None:
    async def scenario() -> None:
        llm = FakeLLMClient(
            [
                _response(
                    {
                        "operations": [
                            {
                                "type": "ADD",
                                "content": "用户以后有机会想去露营",
                                "category": "event",
                                "importance": 2,
                                "originalTimeText": "以后有机会",
                                "eventAt": "2026-08-01T08:00:00Z",
                                "eventTimezone": "Etc/UTC",
                            }
                        ]
                    }
                )
            ]
        )

        operation = (
            await _extractor(llm).extract(
                _turn(),
                rules="保存计划。",
                existing=(),
            )
        )[0]

        assert operation.original_time_text == "以后有机会"
        assert operation.event_at is None
        assert operation.event_timezone is None

    asyncio.run(scenario())


def test_extract_rejects_naive_absolute_iso_candidate_time() -> None:
    async def scenario() -> None:
        llm = FakeLLMClient(
            [
                _response(
                    {
                        "operations": [
                            {
                                "type": "ADD",
                                "content": "用户计划提交报告",
                                "category": "event",
                                "importance": 4,
                                "originalTimeText": "明天",
                                "eventAt": "2026-07-29T15:00:00",
                                "eventTimezone": "Asia/Shanghai",
                            }
                        ]
                    }
                )
            ]
        )

        with pytest.raises(MemoryExtractionError):
            await _extractor(llm).extract(_turn(), rules="保存明确计划。", existing=())

    asyncio.run(scenario())


def test_extract_enforces_input_and_output_bounds_before_unsafe_growth() -> None:
    async def scenario() -> None:
        llm = FakeLLMClient([_response({"operations": []})])
        extractor = _extractor(llm)

        with pytest.raises(ValueError, match="turn"):
            await extractor.extract(
                MemoryTurn("x" * (MAX_TURN_TEXT_LENGTH + 1), "answer"),
                rules="规则",
                existing=(),
            )
        with pytest.raises(ValueError, match="existing"):
            await extractor.extract(
                _turn(),
                rules="规则",
                existing=tuple(_existing() for _ in range(MAX_EXISTING_MEMORIES + 1)),
            )

        assert llm.requests == []

        operations = [
            {
                "type": "NOOP",
            }
            for _ in range(MAX_MEMORY_OPERATIONS + 1)
        ]
        output_llm = FakeLLMClient([_response({"operations": operations})])
        with pytest.raises(MemoryExtractionError):
            await _extractor(output_llm).extract(_turn(), rules="规则", existing=())

    asyncio.run(scenario())


def test_extract_rejects_oversized_serialized_existing_fields_before_llm_call() -> None:
    async def scenario() -> None:
        oversized = "x" * 200_000
        existing = MemoryEntry(
            oversized,
            "bounded content",
            NOW,
            original_time_text=oversized,
        )
        llm = FakeLLMClient([_response({"operations": []})])

        with pytest.raises(ValueError, match="memory extraction input is too large") as exc_info:
            await _extractor(llm).extract(
                _turn(),
                rules="保存长期信息。",
                existing=(existing,),
            )

        assert oversized not in str(exc_info.value)
        assert llm.requests == []

    asyncio.run(scenario())
