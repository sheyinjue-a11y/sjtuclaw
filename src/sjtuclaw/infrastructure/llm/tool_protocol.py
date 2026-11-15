"""Pure provider mappings and strict JSON compatibility protocol parsing."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import NoReturn, cast

from sjtuclaw.errors import ToolProtocolError
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.tool import ToolCall, ToolDefinition, mutable_json
from sjtuclaw.ports.runtime import IdGenerator


def native_tool_definitions(
    definitions: Sequence[ToolDefinition],
) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": _native_tool_description(definition),
                "parameters": mutable_json(definition.input_schema),
            },
        }
        for definition in definitions
    ]


def _completed_tool_call_ids(messages: Sequence[Message]) -> frozenset[str]:
    """Tool calls that have a matching stored tool result."""

    return frozenset(
        message.tool_call_id
        for message in messages
        if message.role is MessageRole.TOOL and message.tool_call_id is not None
    )


def native_messages(
    messages: Sequence[Message],
    definitions: Sequence[ToolDefinition],
) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    completed_ids = _completed_tool_call_ids(messages)
    result_rules = tool_result_usage_prompt(definitions) if definitions else None
    inserted_rules = result_rules is None
    for message in messages:
        if not inserted_rules and message.role is not MessageRole.SYSTEM:
            serialized.append({"role": "system", "content": result_rules})
            inserted_rules = True
        if message.role is MessageRole.ASSISTANT and message.tool_calls:
            completed = [
                call for call in message.tool_calls if call.call_id in completed_ids
            ]
            item: dict[str, object] = {
                "role": "assistant",
                "content": message.content or None,
            }
            if completed:
                # DeepSeek thinking mode requires reasoning_content on
                # assistant messages that carry tool calls; replay an empty
                # string when the provider returned none.
                item["reasoning_content"] = message.reasoning_content or ""
                item["tool_calls"] = [_native_tool_call(call) for call in completed]
            elif message.reasoning_content is not None:
                item["reasoning_content"] = message.reasoning_content
            serialized.append(item)
            continue
        if message.role is MessageRole.TOOL:
            if message.tool_call_id is None:
                raise ToolProtocolError("stored tool message is missing tool_call_id")
            serialized.append(
                {
                    "role": "tool",
                    "content": model_tool_result_content(message),
                    "tool_call_id": message.tool_call_id,
                }
            )
            continue
        item = {"role": message.role.value, "content": message.content}
        if message.role is MessageRole.ASSISTANT and message.reasoning_content is not None:
            item["reasoning_content"] = message.reasoning_content
        serialized.append(item)
    if not inserted_rules:
        serialized.append({"role": "system", "content": result_rules})
    return serialized


def json_messages(
    messages: Sequence[Message],
    definitions: Sequence[ToolDefinition],
) -> list[dict[str, object]]:
    protocol: dict[str, object] = {
        "role": "system",
        "content": json_protocol_prompt(definitions),
    }
    serialized: list[dict[str, object]] = []
    completed_ids = _completed_tool_call_ids(messages)
    inserted_protocol = False
    index = 0
    while index < len(messages):
        message = messages[index]
        if not inserted_protocol and message.role is not MessageRole.SYSTEM:
            serialized.append(protocol)
            inserted_protocol = True
        if message.role is MessageRole.TOOL:
            tool_messages: list[Message] = []
            while index < len(messages) and messages[index].role is MessageRole.TOOL:
                tool_messages.append(messages[index])
                index += 1
            serialized.append(
                {
                    "role": "user",
                    "content": _json_tool_results_envelope(tool_messages),
                }
            )
            continue
        completed = (
            [call for call in message.tool_calls if call.call_id in completed_ids]
            if message.role is MessageRole.ASSISTANT and message.tool_calls
            else []
        )
        if message.role is MessageRole.ASSISTANT and completed:
            item: dict[str, object] = {
                "role": "assistant",
                "content": _json_tool_calls_envelope(completed),
            }
            if message.reasoning_content is not None:
                item["reasoning_content"] = message.reasoning_content
            serialized.append(item)
        else:
            plain_item: dict[str, object] = {
                "role": message.role.value,
                "content": message.content,
            }
            if message.role is MessageRole.ASSISTANT and message.reasoning_content is not None:
                plain_item["reasoning_content"] = message.reasoning_content
            serialized.append(plain_item)
        index += 1
    if not inserted_protocol:
        serialized.append(protocol)
    return serialized


def json_protocol_prompt(definitions: Sequence[ToolDefinition]) -> str:
    tools = [
        {
            "name": definition.name,
            "description": definition.description,
            "input_schema": mutable_json(definition.input_schema),
            "output_schema": mutable_json(definition.output_schema),
            "result_usage": definition.result_usage,
        }
        for definition in definitions
    ]
    serialized_tools = _canonical_json(tools)
    return (
        "You may use only the tools listed below. Return exactly one JSON object and no "
        "Markdown or surrounding text. Allowed outputs are "
        '{"type":"tool_call","tool":"name","args":{}}, '
        '{"type":"tool_calls","calls":[{"tool":"name","args":{}}]}, or '
        '{"type":"final","content":"answer"}. A tool batch must contain 1 to 5 calls. '
        "Earlier assistant tool calls are supplied as tool_calls_history envelopes and "
        "tool results are supplied as user tool_results envelopes. "
        f"{tool_result_usage_prompt(definitions)} Available tools: "
        f"{serialized_tools}"
    )


def model_tool_result_content(message: Message) -> str:
    if message.tool_structured_content is None:
        return message.content
    return json.dumps(
        {
            "content": message.content,
            "structured_content": mutable_json(message.tool_structured_content),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def tool_result_usage_prompt(definitions: Sequence[ToolDefinition]) -> str:
    rules = (
        "Tool result usage rules: content is for explanation; structured_content is "
        "the only machine-readable result field. Host renders actionable resources. "
        "The model must not construct, guess, or rewrite download links and must not "
        "treat internal IDs as URLs. Filesystem paths are not URLs. Only tool-returned, "
        "model-visible fields explicitly marked as authoritative URLs may be used "
        "verbatim. When a result is truncated, the model must not claim that it "
        "received complete content."
    )
    if not definitions:
        return rules
    usage = "; ".join(
        f"{definition.name}: {definition.result_usage}" for definition in definitions
    )
    return f"{rules} Per-tool result usage: {usage}"


def parse_json_response(
    content: str,
    ids: IdGenerator,
) -> tuple[str, tuple[ToolCall, ...]]:
    payload = _strict_json_object(content)
    envelope_type = payload.get("type")
    if envelope_type == "final":
        _require_exact_fields(payload, {"type", "content"})
        final_content = payload["content"]
        if not isinstance(final_content, str) or not final_content.strip():
            raise ToolProtocolError("JSON final content must be non-empty text")
        return final_content, ()

    raw_calls: list[dict[str, object]]
    if envelope_type == "tool_call":
        _require_exact_fields(payload, {"type", "tool", "args"})
        raw_calls = [payload]
    elif envelope_type == "tool_calls":
        _require_exact_fields(payload, {"type", "calls"})
        calls_value = payload["calls"]
        if not isinstance(calls_value, list) or not 1 <= len(calls_value) <= 5:
            raise ToolProtocolError("JSON tool calls must contain 1 to 5 items")
        raw_calls = []
        for value in cast(list[object], calls_value):
            if not isinstance(value, dict):
                raise ToolProtocolError("JSON tool call item must be an object")
            raw_call = cast(dict[str, object], value)
            _require_exact_fields(raw_call, {"tool", "args"})
            raw_calls.append(raw_call)
    else:
        raise ToolProtocolError("JSON response has an unsupported envelope type")

    normalized: list[tuple[str, str]] = []
    for raw_call in raw_calls:
        name = raw_call["tool"]
        arguments = raw_call["args"]
        if not isinstance(name, str) or not name.strip():
            raise ToolProtocolError("JSON tool name must be non-empty text")
        if not isinstance(arguments, dict):
            raise ToolProtocolError("JSON tool args must be an object")
        normalized.append((name, _canonical_json(arguments)))
    return "", tuple(
        ToolCall(ids.new_id("tool_call"), name, arguments_json)
        for name, arguments_json in normalized
    )


def _native_tool_call(call: ToolCall) -> dict[str, object]:
    return {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments_json},
    }


def _native_tool_description(definition: ToolDefinition) -> str:
    return f"{definition.description}\nResult usage: {definition.result_usage}"


def _json_tool_calls_envelope(calls: Sequence[ToolCall]) -> str:
    return _canonical_json(
        {
            "type": "tool_calls_history",
            "calls": [
                {
                    "call_id": call.call_id,
                    "tool": call.name,
                    "arguments_json": call.arguments_json,
                }
                for call in calls
            ],
        }
    )


def _json_tool_results_envelope(messages: Sequence[Message]) -> str:
    return _canonical_json(
        {
            "type": "tool_results",
            "results": [
                {
                    "call_id": message.tool_call_id,
                    "tool": message.tool_name or "legacy_tool",
                    "status": (
                        "success"
                        if message.tool_result_status is None
                        else message.tool_result_status.value
                    ),
                    "content": model_tool_result_content(message),
                    "error_code": message.tool_error_code,
                }
                for message in messages
            ],
        }
    )


def _strict_json_object(content: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(
            content.strip(),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise ToolProtocolError("model returned malformed JSON tool protocol") from None
    if not isinstance(parsed, dict):
        raise ToolProtocolError("JSON tool protocol must be one object")
    return cast(dict[str, object], parsed)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


def _require_exact_fields(payload: Mapping[str, object], expected: set[str]) -> None:
    if set(payload) != expected:
        raise ToolProtocolError("JSON tool protocol has invalid fields")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
