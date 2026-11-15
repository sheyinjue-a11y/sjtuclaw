import json
from datetime import UTC, datetime
from typing import cast

import pytest

from sjtuclaw.errors import ToolProtocolError
from sjtuclaw.infrastructure.llm import tool_protocol
from sjtuclaw.infrastructure.llm.tool_protocol import (
    json_messages,
    json_protocol_prompt,
    native_messages,
    native_tool_definitions,
    parse_json_response,
)
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.tool import (
    ToolCall,
    ToolDefinition,
    ToolResourceLink,
    ToolResultStatus,
    ToolSafetyLevel,
)
from tests.fakes import FakeIdGenerator

NOW = datetime(2026, 7, 15, tzinfo=UTC)
DEFINITION = ToolDefinition(
    name="read_file",
    description="Read a UTF-8 file.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ("path",),
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"filename": {"type": "string"}},
        "required": ("filename",),
        "additionalProperties": False,
    },
    result_usage="Use the file content.",
    safety_level=ToolSafetyLevel.READ_ONLY,
)


def test_native_mapping_preserves_tools_calls_results_and_reasoning() -> None:
    call = ToolCall("call_001", "read_file", '{"path":"README.md"}')
    messages = (
        Message("system", MessageRole.SYSTEM, "rules", NOW, 0),
        Message(
            "assistant",
            MessageRole.ASSISTANT,
            "",
            NOW,
            1,
            tool_calls=(call,),
            reasoning_content="reasoning that must be replayed",
        ),
        Message(
            "tool",
            MessageRole.TOOL,
            "contents",
            NOW,
            2,
            tool_call_id="call_001",
            tool_name="read_file",
            tool_result_status=ToolResultStatus.SUCCESS,
        ),
    )

    assert native_tool_definitions((DEFINITION,)) == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 file.\n"
                    "Result usage: Use the file content."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    assert native_messages(messages, ()) == [
        {"role": "system", "content": "rules"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "reasoning that must be replayed",
            "tool_calls": [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                }
            ],
        },
        {"role": "tool", "content": "contents", "tool_call_id": "call_001"},
    ]


def test_native_mapping_drops_orphan_tool_calls_without_results() -> None:
    orphan = ToolCall("call_orphan", "write_file", '{"path":"notes.md"}')
    completed = ToolCall("call_done", "read_file", '{"path":"notes.md"}')
    messages = (
        Message(
            "assistant",
            MessageRole.ASSISTANT,
            "working",
            NOW,
            1,
            tool_calls=(orphan, completed),
        ),
        Message(
            "tool",
            MessageRole.TOOL,
            "contents",
            NOW,
            2,
            tool_call_id="call_done",
            tool_name="read_file",
            tool_result_status=ToolResultStatus.SUCCESS,
        ),
    )

    serialized = native_messages(messages, ())
    assistant = next(item for item in serialized if item["role"] == "assistant")
    calls = cast(list[dict[str, object]], assistant["tool_calls"])
    assert [call["id"] for call in calls] == ["call_done"]


def test_native_mapping_drops_all_orphan_tool_calls_and_keeps_text() -> None:
    orphan = ToolCall("call_orphan", "write_file", '{"path":"notes.md"}')
    messages = (
        Message(
            "assistant",
            MessageRole.ASSISTANT,
            "pending write",
            NOW,
            1,
            tool_calls=(orphan,),
        ),
    )

    assert native_messages(messages, ()) == [
        {"role": "assistant", "content": "pending write"},
    ]


def test_native_mapping_replays_empty_reasoning_for_tool_call_messages() -> None:
    call = ToolCall("call_001", "read_file", '{"path":"README.md"}')
    messages = (
        Message(
            "assistant",
            MessageRole.ASSISTANT,
            "",
            NOW,
            1,
            tool_calls=(call,),
        ),
        Message(
            "tool",
            MessageRole.TOOL,
            "contents",
            NOW,
            2,
            tool_call_id="call_001",
            tool_name="read_file",
            tool_result_status=ToolResultStatus.SUCCESS,
        ),
    )

    serialized = native_messages(messages, ())
    assistant = next(item for item in serialized if item["role"] == "assistant")
    assert assistant["reasoning_content"] == ""
    calls = cast(list[dict[str, object]], assistant["tool_calls"])
    assert calls[0]["id"] == "call_001"


def test_typed_tool_result_projection_is_identical_and_resource_free() -> None:
    message = Message(
        "tool",
        MessageRole.TOOL,
        "下载快照已创建。",
        NOW,
        2,
        tool_call_id="call_001",
        tool_name="read_file",
        tool_result_status=ToolResultStatus.SUCCESS,
        tool_structured_content={"filename": "report.md", "size_bytes": 12},
        tool_resources=(
            ToolResourceLink(
                "download",
                "report.md",
                "/downloads/download_123",
                "text/markdown",
            ),
        ),
    )
    expected = (
        '{"content":"下载快照已创建。",'
        '"structured_content":{"filename":"report.md","size_bytes":12}}'
    )

    assert tool_protocol.model_tool_result_content(message) == expected
    native = native_messages((message,), (DEFINITION,))
    native_result = next(item for item in native if item["role"] == "tool")
    assert native_result["content"] == expected
    json_protocol = json_messages((message,), (DEFINITION,))
    envelope = json.loads(str(json_protocol[-1]["content"]))
    assert envelope["results"][0]["content"] == expected

    serialized = json.dumps(
        {"native": native, "json": json_protocol},
        ensure_ascii=False,
    )
    assert "tool_resources" not in serialized
    assert '"resources"' not in serialized
    assert "href" not in serialized
    assert "download_123" not in serialized


def test_legacy_tool_result_projection_preserves_original_content() -> None:
    message = Message(
        "legacy",
        MessageRole.TOOL,
        "legacy contents",
        NOW,
        1,
        tool_call_id="call_legacy",
    )

    assert tool_protocol.model_tool_result_content(message) == "legacy contents"
    native_result = native_messages((message,), (DEFINITION,))[-1]
    assert native_result["content"] == "legacy contents"
    envelope = json.loads(str(json_messages((message,), (DEFINITION,))[-1]["content"]))
    assert envelope["results"][0]["content"] == "legacy contents"


def test_protocol_prompts_include_schemas_result_usage_and_shared_rules() -> None:
    prompt = json_protocol_prompt((DEFINITION,))
    shared_rules = tool_protocol.tool_result_usage_prompt((DEFINITION,))
    native = native_messages(
        (Message("user", MessageRole.USER, "read it", NOW, 1),),
        (DEFINITION,),
    )

    assert '"output_schema"' in prompt
    assert '"result_usage":"Use the file content."' in prompt
    assert "structured_content is the only machine-readable result field" in shared_rules
    assert "Host renders actionable resources" in shared_rules
    assert "must not construct, guess, or rewrite download links" in shared_rules
    assert "must not treat internal IDs as URLs" in shared_rules
    assert "Filesystem paths are not URLs." in shared_rules
    assert (
        "Only tool-returned, model-visible fields explicitly marked as authoritative "
        "URLs may be used verbatim."
    ) in shared_rules
    assert (
        "When a result is truncated, the model must not claim that it received "
        "complete content."
    ) in shared_rules
    assert {"role": "system", "content": shared_rules} in native


def test_json_history_uses_protocol_and_never_native_tool_role() -> None:
    call = ToolCall("call_001", "read_file", '{"path":"README.md"}')
    messages = (
        Message("system", MessageRole.SYSTEM, "rules", NOW, 0),
        Message("user", MessageRole.USER, "read it", NOW, 1),
        Message(
            "assistant",
            MessageRole.ASSISTANT,
            "",
            NOW,
            2,
            tool_calls=(call,),
            reasoning_content="hidden",
        ),
        Message(
            "tool",
            MessageRole.TOOL,
            "contents",
            NOW,
            3,
            tool_call_id="call_001",
            tool_name="read_file",
            tool_result_status=ToolResultStatus.SUCCESS,
        ),
    )

    serialized = json_messages(messages, (DEFINITION,))

    assert [message["role"] for message in serialized] == [
        "system",
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "Available tools" in str(serialized[1]["content"])
    assert "tool_calls_history" in str(serialized[1]["content"])
    assert "tool_calls_history" in str(serialized[3]["content"])
    assert serialized[3]["reasoning_content"] == "hidden"
    assert "tool_results" in str(serialized[4]["content"])


def test_strict_json_parser_normalizes_all_supported_outputs_after_validation() -> None:
    ids = FakeIdGenerator()

    final, calls = parse_json_response(
        '  {"type":"final","content":"done"}\n',
        ids,
    )
    assert (final, calls) == ("done", ())

    final, calls = parse_json_response(
        '{"type":"tool_calls","calls":['
        '{"tool":"read_file","args":{"path":"README.md"}},'
        '{"tool":"read_file","args":{"path":"pyproject.toml"}}]}',
        ids,
    )
    assert final == ""
    assert calls == (
        ToolCall("tool_call_001", "read_file", '{"path":"README.md"}'),
        ToolCall("tool_call_002", "read_file", '{"path":"pyproject.toml"}'),
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "```json\n{}\n```",
        '{"type":"final","content":"ok"} trailing',
        '{"type":"final","content":"a","content":"b"}',
        '{"type":"final","content":""}',
        '{"type":"final","content":"ok","extra":true}',
        '{"type":"tool_call","tool":"read_file","args":[]}',
        '{"type":"tool_calls","calls":[]}',
        '{"type":"tool_calls","calls":['
        '{"tool":"x","args":{}},{"tool":"x","args":{}},'
        '{"tool":"x","args":{}},{"tool":"x","args":{}},'
        '{"tool":"x","args":{}},{"tool":"x","args":{}}]}',
        '{"type":"tool_call","tool":"x","args":{"value":NaN}}',
        "[]",
    ],
)
def test_strict_json_parser_rejects_malformed_or_non_exact_payloads(payload: str) -> None:
    with pytest.raises(ToolProtocolError):
        parse_json_response(payload, FakeIdGenerator())
