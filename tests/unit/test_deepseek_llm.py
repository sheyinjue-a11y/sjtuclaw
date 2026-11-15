from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest

from sjtuclaw.config import Settings
from sjtuclaw.errors import LLMError, ToolProtocolError
from sjtuclaw.infrastructure.llm.deepseek import DeepSeekLLMClient
from sjtuclaw.models.llm import (
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMToolMode,
    LLMUsage,
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

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
SECRET = "deepseek-secret-test-key"
TOOL = ToolDefinition(
    name="current_time",
    description="Return current time.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema={"type": "object", "properties": {}, "additionalProperties": False},
    result_usage="Use the current time.",
    safety_level=ToolSafetyLevel.READ_ONLY,
)


def _settings() -> Settings:
    return Settings(api_key=SECRET, base_url="https://deepseek.example/v1", model="test-model")


def _request() -> LLMRequest:
    return LLMRequest(
        messages=(
            Message("msg_001", MessageRole.SYSTEM, "Be concise.", NOW, 1),
            Message("msg_002", MessageRole.USER, "Hello", NOW, 2),
            Message("msg_003", MessageRole.ASSISTANT, "Hi", NOW, 3),
        )
    )


def _success_payload() -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello back"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


def test_llm_models_reject_empty_requests_and_negative_usage() -> None:
    with pytest.raises(ValueError, match="at least one message"):
        LLMRequest(())
    with pytest.raises(ValueError, match="must not be negative"):
        LLMUsage(-1, 0, 0)


async def _complete_and_close(
    adapter: DeepSeekLLMClient,
    request: LLMRequest | None = None,
) -> LLMResponse:
    try:
        return await adapter.complete(_request() if request is None else request)
    finally:
        await adapter.aclose()


async def _stream_and_close(
    adapter: DeepSeekLLMClient,
    request: LLMRequest | None = None,
) -> list[LLMStreamEvent]:
    try:
        return [event async for event in adapter.stream(_request() if request is None else request)]
    finally:
        await adapter.aclose()


def test_complete_sends_text_messages_and_returns_usage() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_success_payload())

    adapter = DeepSeekLLMClient(_settings(), transport=httpx.MockTransport(handler))

    response = asyncio.run(_complete_and_close(adapter))

    assert response == LLMResponse("Hello back", LLMUsage(11, 3, 14), "stop")
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://deepseek.example/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in str(request.url)
    payload = cast(dict[str, object], json.loads(request.content))
    assert payload == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ],
        "stream": False,
        "thinking": {"type": "enabled"},
    }


def test_complete_sends_disabled_thinking_mode() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_success_payload())

    adapter = DeepSeekLLMClient(
        _settings(),
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(_complete_and_close(adapter))

    assert json.loads(requests[0].content)["thinking"] == {"type": "disabled"}


def test_stream_yields_visible_deltas_and_one_assembled_response() -> None:
    requests: list[httpx.Request] = []
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "Hello"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "delta": {"content": " back", "reasoning_content": "private"},
                    "finish_reason": "stop",
                }
            ]
        },
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 3,
                "total_tokens": 14,
                "prompt_cache_hit_tokens": 7,
                "prompt_cache_miss_tokens": 4,
            },
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body)

    adapter = DeepSeekLLMClient(_settings(), transport=httpx.MockTransport(handler))
    events = asyncio.run(_stream_and_close(adapter))

    assert [event.content_delta for event in events[:-1]] == ["Hello", " back"]
    assert events[-1].response == LLMResponse(
        "Hello back",
        LLMUsage(11, 3, 14, 7, 4),
        "stop",
        reasoning_content="private",
    )
    payload = cast(dict[str, object], json.loads(requests[0].content))
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["thinking"] == {"type": "enabled"}


def test_stream_assembles_native_tool_fragments_before_completion() -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_",
                                "type": "function",
                                "function": {"name": "current_", "arguments": "{"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "001",
                                "function": {"name": "time", "arguments": "}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    adapter = DeepSeekLLMClient(
        _settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )
    request = LLMRequest(_request().messages, (TOOL,), LLMToolMode.NATIVE)

    events = asyncio.run(_stream_and_close(adapter, request))

    assert [event.content_delta for event in events[:-1]] == []
    assert events[-1].response is not None
    assert events[-1].response.tool_calls == (ToolCall("call_001", "current_time", "{}"),)


def test_stream_hides_json_tool_protocol_fragments_from_visible_deltas() -> None:
    content = '{"type":"tool_call","tool":"current_time","args":{}}'
    chunks = [
        {"choices": [{"delta": {"content": content[:23]}, "finish_reason": None}]},
        {
            "choices": [
                {"delta": {"content": content[23:]}, "finish_reason": "stop"},
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 18, "completion_tokens": 3, "total_tokens": 21}},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    adapter = DeepSeekLLMClient(
        Settings(
            api_key=SECRET,
            base_url="https://deepseek.example/v1",
            model="test-model",
            tool_mode="json",
        ),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
        ids=FakeIdGenerator(),
    )
    request = LLMRequest(_request().messages, (TOOL,), LLMToolMode.JSON)

    events = asyncio.run(_stream_and_close(adapter, request))

    assert [event.content_delta for event in events[:-1]] == []
    assert events[-1].response is not None
    assert events[-1].response.tool_calls == (ToolCall("tool_call_001", "current_time", "{}"),)


def test_native_mode_sends_definitions_and_parses_calls_with_reasoning() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "must replay this",
                            "tool_calls": [
                                {
                                    "id": "call_001",
                                    "type": "function",
                                    "function": {
                                        "name": "current_time",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            },
        )

    adapter = DeepSeekLLMClient(_settings(), transport=httpx.MockTransport(handler))
    request = LLMRequest(_request().messages, (TOOL,), LLMToolMode.NATIVE)

    response = asyncio.run(_complete_and_close(adapter, request))

    assert response.tool_calls == (ToolCall("call_001", "current_time", "{}"),)
    assert response.reasoning_content == "must replay this"
    payload = cast(dict[str, object], json.loads(requests[0].content))
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "current_time",
                "description": (
                    "Return current time.\n"
                    "Result usage: Use the current time."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
    ]


@pytest.mark.parametrize("mode", (LLMToolMode.NATIVE, LLMToolMode.JSON))
def test_tool_result_requests_use_shared_resource_free_projection(
    mode: LLMToolMode,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response_payload = _success_payload()
        if mode is LLMToolMode.JSON:
            choices = cast(list[dict[str, object]], response_payload["choices"])
            response_message = cast(dict[str, object], choices[0]["message"])
            response_message["content"] = '{"type":"final","content":"Hello back"}'
        return httpx.Response(200, json=response_payload)

    call = ToolCall("call_001", "current_time", "{}")
    messages = (
        Message("system", MessageRole.SYSTEM, "Be concise.", NOW, 0),
        Message("user", MessageRole.USER, "What time is it?", NOW, 1),
        Message(
            "assistant",
            MessageRole.ASSISTANT,
            "",
            NOW,
            2,
            tool_calls=(call,),
        ),
        Message(
            "tool",
            MessageRole.TOOL,
            "time captured",
            NOW,
            3,
            tool_call_id=call.call_id,
            tool_name=call.name,
            tool_result_status=ToolResultStatus.SUCCESS,
            tool_structured_content={"timezone": "Asia/Shanghai"},
            tool_resources=(
                ToolResourceLink(
                    "download",
                    "time.txt",
                    "/downloads/download_deepseek_secret",
                ),
            ),
        ),
    )
    adapter = DeepSeekLLMClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        ids=FakeIdGenerator(),
    )
    request = LLMRequest(messages, (TOOL,), mode)

    asyncio.run(_complete_and_close(adapter, request))

    serialized_request = requests[0].content.decode()
    assert "download_deepseek_secret" not in serialized_request
    assert '"href"' not in serialized_request
    assert '"tool_resources"' not in serialized_request
    assert "Host renders actionable resources" in serialized_request
    payload = cast(dict[str, object], json.loads(serialized_request))
    serialized_messages = cast(list[dict[str, object]], payload["messages"])
    expected = (
        '{"content":"time captured",'
        '"structured_content":{"timezone":"Asia/Shanghai"}}'
    )
    if mode is LLMToolMode.NATIVE:
        result_message = next(
            message for message in serialized_messages if message["role"] == "tool"
        )
        assert result_message["content"] == expected
    else:
        envelopes = [
            json.loads(str(message["content"]))
            for message in serialized_messages
            if message["role"] == "user" and str(message["content"]).startswith("{")
        ]
        result_envelope = next(
            envelope for envelope in envelopes if envelope.get("type") == "tool_results"
        )
        assert result_envelope["results"][0]["content"] == expected


def test_native_mode_leaves_malformed_arguments_for_tool_batch_validation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_001",
                                    "type": "function",
                                    "function": {
                                        "name": "current_time",
                                        "arguments": "{malformed",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            },
        )

    adapter = DeepSeekLLMClient(_settings(), transport=httpx.MockTransport(handler))
    request = LLMRequest(_request().messages, (TOOL,), LLMToolMode.NATIVE)

    response = asyncio.run(_complete_and_close(adapter, request))

    assert response.tool_calls == (ToolCall("call_001", "current_time", "{malformed"),)


@pytest.mark.parametrize(
    "tool_calls",
    [
        {"not": "an array"},
        [None],
        [
            {
                "id": "call_001",
                "type": "function",
                "function": [],
            }
        ],
        [
            {
                "id": "call_001",
                "type": "function",
                "function": {"name": "current_time", "arguments": None},
            }
        ],
    ],
)
def test_native_mode_maps_malformed_tool_call_structure_to_protocol_error(
    tool_calls: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            },
        )

    adapter = DeepSeekLLMClient(_settings(), transport=httpx.MockTransport(handler))
    request = LLMRequest(_request().messages, (TOOL,), LLMToolMode.NATIVE)

    with pytest.raises(ToolProtocolError):
        asyncio.run(_complete_and_close(adapter, request))


def test_json_mode_uses_strict_text_protocol_and_generated_call_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                '{"type":"tool_call","tool":"current_time","args":{}}'
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 18, "completion_tokens": 3, "total_tokens": 21},
            },
        )

    settings = Settings(
        api_key=SECRET,
        base_url="https://deepseek.example/v1",
        model="test-model",
        tool_mode="json",
    )
    adapter = DeepSeekLLMClient(
        settings,
        transport=httpx.MockTransport(handler),
        ids=FakeIdGenerator(),
    )
    request = LLMRequest(_request().messages, (TOOL,), LLMToolMode.JSON)

    response = asyncio.run(_complete_and_close(adapter, request))

    assert response.tool_calls == (ToolCall("tool_call_001", "current_time", "{}"),)
    payload = cast(dict[str, object], json.loads(requests[0].content))
    assert "tools" not in payload
    assert payload["response_format"] == {"type": "json_object"}
    messages = cast(list[dict[str, object]], payload["messages"])
    assert any("Available tools" in str(message["content"]) for message in messages)


def test_json_mode_malformed_payload_fails_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        payload = _success_payload()
        choices = cast(list[dict[str, object]], payload["choices"])
        message = cast(dict[str, object], choices[0]["message"])
        message["content"] = "```json\n{}\n```"
        return httpx.Response(200, json=payload)

    settings = Settings(api_key=SECRET, tool_mode="json")
    adapter = DeepSeekLLMClient(
        settings,
        transport=httpx.MockTransport(handler),
        ids=FakeIdGenerator(),
    )
    request = LLMRequest(_request().messages, (TOOL,), LLMToolMode.JSON)

    with pytest.raises(ToolProtocolError):
        asyncio.run(_complete_and_close(adapter, request))

    assert attempts == 1


@pytest.mark.parametrize("status_code", [429, 500, 501, 502, 503, 504, 599])
def test_retryable_http_status_then_success(status_code: int) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status_code, json={"error": "temporary"})
        return httpx.Response(200, json=_success_payload())

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = DeepSeekLLMClient(
        _settings(), transport=httpx.MockTransport(handler), sleep=sleep
    )

    response = asyncio.run(_complete_and_close(adapter))

    assert response.content == "Hello back"
    assert attempts == 2
    assert sleeps == [0.5]


def test_timeout_then_success() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("temporary timeout", request=request)
        return httpx.Response(200, json=_success_payload())

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = DeepSeekLLMClient(
        _settings(), transport=httpx.MockTransport(handler), sleep=sleep
    )

    response = asyncio.run(_complete_and_close(adapter))

    assert response.content == "Hello back"
    assert attempts == 2
    assert sleeps == [0.5]


def test_insufficient_system_resource_then_success() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": None},
                            "finish_reason": "insufficient_system_resource",
                        }
                    ]
                },
            )
        return httpx.Response(200, json=_success_payload())

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = DeepSeekLLMClient(
        _settings(), transport=httpx.MockTransport(handler), sleep=sleep
    )

    response = asyncio.run(_complete_and_close(adapter))

    assert response.content == "Hello back"
    assert attempts == 2
    assert sleeps == [0.5]


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_non_retryable_http_error_does_not_retry_or_leak_secret(
    status_code: int,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, json={"error": f"rejected {SECRET}"})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = DeepSeekLLMClient(
        _settings(), transport=httpx.MockTransport(handler), sleep=sleep
    )

    with pytest.raises(LLMError) as caught:
        asyncio.run(_complete_and_close(adapter))

    assert attempts == 1
    assert sleeps == []
    assert str(status_code) in str(caught.value)
    assert SECRET not in str(caught.value)


def test_retryable_http_error_stops_after_two_retries() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "temporary"})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = DeepSeekLLMClient(
        _settings(), transport=httpx.MockTransport(handler), sleep=sleep
    )

    with pytest.raises(LLMError, match="after retryable HTTP status 503"):
        asyncio.run(_complete_and_close(adapter))

    assert attempts == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {
            "choices": [{"message": {"content": 42}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
        },
        {
            "choices": [
                {
                    "message": {"role": "user", "content": "not an assistant"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ],
)
def test_malformed_success_response_fails_without_retry(payload: dict[str, object]) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json=payload)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = DeepSeekLLMClient(
        _settings(), transport=httpx.MockTransport(handler), sleep=sleep
    )

    with pytest.raises(LLMError, match="malformed"):
        asyncio.run(_complete_and_close(adapter))

    assert attempts == 1
    assert sleeps == []


def test_transport_error_text_does_not_leak_secret() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError(f"failed with {SECRET}", request=request)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = DeepSeekLLMClient(
        _settings(), transport=httpx.MockTransport(handler), sleep=sleep
    )

    with pytest.raises(LLMError) as caught:
        asyncio.run(_complete_and_close(adapter))

    assert attempts == 3
    assert sleeps == [0.5, 1.0]
    assert SECRET not in str(caught.value)


def test_adapter_does_not_close_an_injected_http_client() -> None:
    async def assert_ownership() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        adapter = DeepSeekLLMClient(_settings(), client=client)

        await adapter.aclose()

        assert not client.is_closed
        await client.aclose()

    asyncio.run(assert_ownership())
