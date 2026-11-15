import json
from datetime import UTC, datetime
from math import ceil

from sjtuclaw.infrastructure.tokenizers import ApproximateTokenCounter
from sjtuclaw.models.llm import LLMRequest, LLMToolMode
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.tool import ToolDefinition, ToolSafetyLevel
from sjtuclaw.ports.token_counter import TokenCounter

NOW = datetime(2026, 7, 15, tzinfo=UTC)


def _count(counter: TokenCounter, text: str) -> int:
    request = LLMRequest((Message("message_001", MessageRole.USER, text, NOW, 1),))
    return counter.count(request)


def test_approximate_counter_is_deterministic_and_counts_utf8_payload() -> None:
    counter = ApproximateTokenCounter()

    short = _count(counter, "hello")
    long = _count(counter, "hello" * 20)
    chinese = _count(counter, "你好" * 20)

    assert short == _count(counter, "hello")
    assert long > short
    assert chinese > short


def test_approximate_counter_includes_tool_definitions_and_json_protocol() -> None:
    definition = ToolDefinition(
        name="current_time",
        description="Return current time.",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}, "additionalProperties": False},
        result_usage="Use the current time.",
        safety_level=ToolSafetyLevel.READ_ONLY,
    )
    plain = LLMRequest((Message("message_001", MessageRole.SYSTEM, "rules", NOW, 0),))
    native = LLMRequest(plain.messages, (definition,), LLMToolMode.NATIVE)
    json_mode = LLMRequest(plain.messages, (definition,), LLMToolMode.JSON)

    assert ApproximateTokenCounter().count(native) > ApproximateTokenCounter().count(plain)
    assert ApproximateTokenCounter().count(json_mode) > ApproximateTokenCounter().count(plain)


def test_approximate_native_count_includes_shared_result_rules_and_usage() -> None:
    definition = ToolDefinition(
        name="current_time",
        description="Return current time.",
        input_schema={"type": "object", "properties": {}},
        output_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        result_usage="Use the current time.",
        safety_level=ToolSafetyLevel.READ_ONLY,
    )
    request = LLMRequest(
        (Message("message_001", MessageRole.SYSTEM, "rules", NOW, 0),),
        (definition,),
        LLMToolMode.NATIVE,
    )
    expected_payload = {
        "messages": [
            {"role": "system", "content": "rules"},
            {
                "role": "system",
                "content": (
                    "Tool result usage rules: content is for explanation; "
                    "structured_content is the only machine-readable result field. "
                    "Host renders actionable resources. The model must not construct, "
                    "guess, or rewrite download links and must not treat internal IDs as "
                    "URLs. Filesystem paths are not URLs. Only tool-returned, "
                    "model-visible fields explicitly marked as authoritative URLs may "
                    "be used verbatim. When a result is truncated, the model must not "
                    "claim that it received complete content. Per-tool result usage: "
                    "current_time: Use the current time."
                ),
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "current_time",
                    "description": (
                        "Return current time.\n"
                        "Result usage: Use the current time."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    serialized = json.dumps(
        expected_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert ApproximateTokenCounter().count(request) == ceil(len(serialized) / 4)
