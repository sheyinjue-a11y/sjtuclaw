from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx

from sjtuclaw.config import ProviderConfiguration, ProviderKind
from sjtuclaw.infrastructure.llm.anthropic import AnthropicLLMClient
from sjtuclaw.infrastructure.llm.openai_compatible import OpenAICompatibleLLMClient
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMStreamEvent, LLMToolMode, LLMUsage
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.tool import (
    ToolCall,
    ToolDefinition,
    ToolResourceLink,
    ToolResultStatus,
    ToolSafetyLevel,
)

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
SECRET = "provider-secret-key"


def _request() -> LLMRequest:
    return LLMRequest(
        (
            Message("system", MessageRole.SYSTEM, "Be concise.", NOW, 0),
            Message("user", MessageRole.USER, "Hello", NOW, 1),
        )
    )


def _configuration(provider: ProviderKind, base_url: str) -> ProviderConfiguration:
    return ProviderConfiguration(
        provider=provider,
        model="test-model",
        base_url=base_url,
        api_key=SECRET,
    )


def test_openai_compatible_complete_uses_chat_completions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello back"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    client = OpenAICompatibleLLMClient(
        _configuration(ProviderKind.OPENAI_COMPATIBLE, "https://openai.example/v1"),
        transport=httpx.MockTransport(handler),
    )
    try:
        response = asyncio.run(client.complete(_request()))
    finally:
        asyncio.run(client.aclose())

    assert response == LLMResponse("Hello back", LLMUsage(5, 2, 7), "stop")
    assert str(requests[0].url) == "https://openai.example/v1/chat/completions"
    assert requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert "thinking" not in json.loads(requests[0].content)


def test_openai_compatible_stream_does_not_send_thinking() -> None:
    requests: list[httpx.Request] = []
    chunks = [
        {"choices": [{"delta": {"content": "Hello back"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body)

    client = OpenAICompatibleLLMClient(
        _configuration(ProviderKind.OPENAI_COMPATIBLE, "https://openai.example/v1"),
        transport=httpx.MockTransport(handler),
    )

    async def stream_and_close() -> list[LLMStreamEvent]:
        try:
            return [event async for event in client.stream(_request())]
        finally:
            await client.aclose()

    events = asyncio.run(stream_and_close())

    assert events[-1].response == LLMResponse("Hello back", LLMUsage(5, 2, 7), "stop")
    assert "thinking" not in json.loads(requests[0].content)


def test_anthropic_complete_uses_messages_without_exposing_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Hello back"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    client = AnthropicLLMClient(
        _configuration(ProviderKind.ANTHROPIC, "https://anthropic.example"),
        transport=httpx.MockTransport(handler),
    )
    try:
        response = asyncio.run(client.complete(_request()))
    finally:
        asyncio.run(client.aclose())

    assert response == LLMResponse("Hello back", LLMUsage(5, 2, 7), "end_turn")
    assert str(requests[0].url) == "https://anthropic.example/v1/messages"
    assert requests[0].headers["x-api-key"] == SECRET
    payload = json.loads(requests[0].content)
    assert payload["system"] == "Be concise."
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]


async def _stream_and_close(client: AnthropicLLMClient) -> list[LLMStreamEvent]:
    try:
        return [event async for event in client.stream(_request())]
    finally:
        await client.aclose()


def test_anthropic_stream_yields_deltas_and_assembled_response() -> None:
    chunks: list[dict[str, object]] = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " back"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {chunk['type']}\ndata: {json.dumps(chunk)}\n\n" for chunk in chunks)

    client = AnthropicLLMClient(
        _configuration(ProviderKind.ANTHROPIC, "https://anthropic.example"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )

    events = asyncio.run(_stream_and_close(client))

    assert [event.content_delta for event in events[:-1]] == ["Hello", " back"]
    assert events[-1].response == LLMResponse("Hello back", LLMUsage(5, 2, 7), "end_turn")


def test_anthropic_native_tools_round_trip() -> None:
    tool = ToolDefinition(
        name="list_dir",
        description="List files.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        output_schema={"type": "object", "properties": {}, "additionalProperties": False},
        result_usage="Use the directory listing.",
        safety_level=ToolSafetyLevel.READ_ONLY,
    )
    prior_call = ToolCall("toolu_prior", "list_dir", '{"path":"."}')
    request = LLMRequest(
        (
            *_request().messages,
            Message(
                "assistant",
                MessageRole.ASSISTANT,
                "",
                NOW,
                2,
                tool_calls=(prior_call,),
            ),
            Message(
                "tool",
                MessageRole.TOOL,
                "listing captured",
                NOW,
                3,
                tool_call_id=prior_call.call_id,
                tool_name=prior_call.name,
                tool_result_status=ToolResultStatus.SUCCESS,
                tool_structured_content={"entries": ("report.md",)},
                tool_resources=(
                    ToolResourceLink(
                        "download",
                        "report.md",
                        "/downloads/download_anthropic_secret",
                    ),
                ),
            ),
        ),
        tools=(tool,),
        tool_mode=LLMToolMode.NATIVE,
    )
    captured: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        captured.append(incoming)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "list_dir", "input": {"path": "."}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    client = AnthropicLLMClient(
        _configuration(ProviderKind.ANTHROPIC, "https://anthropic.example"),
        transport=httpx.MockTransport(handler),
    )
    try:
        response = asyncio.run(client.complete(request))
    finally:
        asyncio.run(client.aclose())

    assert response.tool_calls[0].name == "list_dir"
    assert response.tool_calls[0].arguments_json == '{"path":"."}'
    serialized_request = captured[0].content.decode()
    assert "download_anthropic_secret" not in serialized_request
    assert '"href"' not in serialized_request
    assert '"tool_resources"' not in serialized_request
    payload = json.loads(serialized_request)
    assert "Host renders actionable resources" in payload["system"]
    assert payload["messages"][-1]["content"][0]["content"] == (
        '{"content":"listing captured",'
        '"structured_content":{"entries":["report.md"]}}'
    )
    assert payload["tools"] == [
        {
            "name": "list_dir",
            "description": (
                "List files.\n"
                "Result usage: Use the directory listing."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }
    ]
