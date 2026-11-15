"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import cast

import httpx

from sjtuclaw.config import ProviderConfiguration
from sjtuclaw.errors import LLMError, ToolProtocolError
from sjtuclaw.infrastructure.llm.tool_protocol import (
    model_tool_result_content,
    tool_result_usage_prompt,
)
from sjtuclaw.models.llm import (
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMToolMode,
    LLMUsage,
)
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.tool import ToolCall, ToolDefinition, mutable_json
from sjtuclaw.ports.runtime import IdGenerator


class AnthropicLLMClient:
    """Translate the provider-independent contract to Anthropic Messages."""

    def __init__(
        self,
        configuration: ProviderConfiguration,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport cannot both be provided")
        self._configuration = configuration
        self._client = client or httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        self._owns_client = client is None
        self._ids = ids
        self._endpoint = f"{configuration.base_url.rstrip('/')}/v1/messages"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            response = await self._client.post(
                self._endpoint,
                headers=self._headers(),
                json=self._payload(request, stream=False),
            )
        except httpx.HTTPError:
            raise LLMError("Anthropic request failed before receiving a response") from None
        if response.status_code != 200:
            raise LLMError(f"Anthropic request failed with HTTP status {response.status_code}")
        return _response_from_payload(response.json(), self._ids)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        text: list[str] = []
        tool_blocks: dict[int, dict[str, str]] = {}
        usage: LLMUsage | None = None
        finish_reason: str | None = None
        try:
            async with self._client.stream(
                "POST",
                self._endpoint,
                headers=self._headers(),
                json=self._payload(request, stream=True),
            ) as response:
                if response.status_code != 200:
                    raise LLMError(
                        f"Anthropic request failed with HTTP status {response.status_code}"
                    )
                async for payload in _sse_payloads(response):
                    event_type = payload.get("type")
                    if event_type == "message_start":
                        message = _mapping(payload.get("message"))
                        usage = _usage_from_mapping(_mapping(message.get("usage")))
                    elif event_type == "content_block_start":
                        index = _index(payload)
                        block = _mapping(payload.get("content_block"))
                        kind = _text(block.get("type"))
                        if kind == "tool_use":
                            tool_blocks[index] = {
                                "id": _text(block.get("id")),
                                "name": _text(block.get("name")),
                                "input": "",
                            }
                    elif event_type == "content_block_delta":
                        index = _index(payload)
                        delta = _mapping(payload.get("delta"))
                        kind = _text(delta.get("type"))
                        if kind == "text_delta":
                            delta_text = _text(delta.get("text"))
                            if delta_text:
                                text.append(delta_text)
                                yield LLMStreamEvent(content_delta=delta_text)
                        elif kind == "input_json_delta" and index in tool_blocks:
                            tool_blocks[index]["input"] += _text(delta.get("partial_json"))
                    elif event_type == "message_delta":
                        delta = _mapping(payload.get("delta"))
                        finish_reason = _optional_text(delta.get("stop_reason"))
                        delta_usage = _mapping(payload.get("usage"))
                        if delta_usage:
                            usage = _usage_from_mapping(delta_usage, prior=usage)
        except httpx.HTTPError:
            raise LLMError("Anthropic request failed before receiving a response") from None

        if usage is None:
            raise LLMError("Anthropic stream ended without usage")
        calls = tuple(
            ToolCall(
                call_id=block["id"],
                name=block["name"],
                arguments_json=block["input"] or "{}",
            )
            for _, block in sorted(tool_blocks.items())
        )
        yield LLMStreamEvent(
            response=LLMResponse("".join(text), usage, finish_reason, calls)
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._configuration.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _payload(self, request: LLMRequest, *, stream: bool) -> dict[str, object]:
        system, messages = _messages(request.messages, request.tools)
        payload: dict[str, object] = {
            "model": self._configuration.model,
            "messages": messages,
            "max_tokens": 4096,
            "stream": stream,
        }
        if system:
            payload["system"] = system
        if request.tools and request.tool_mode is LLMToolMode.NATIVE:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": (
                        f"{tool.description}\nResult usage: {tool.result_usage}"
                    ),
                    "input_schema": mutable_json(tool.input_schema),
                }
                for tool in request.tools
            ]
        elif request.tools and request.tool_mode is LLMToolMode.JSON:
            payload["system"] = (
                f"{system}\nReturn a JSON object matching the requested action."
                if system
                else "Return a JSON object matching the requested action."
            )
        return payload


def _messages(
    messages: tuple[Message, ...],
    definitions: tuple[ToolDefinition, ...],
) -> tuple[str, list[dict[str, object]]]:
    system_parts: list[str] = []
    result: list[dict[str, object]] = []
    for message in messages:
        if message.role is MessageRole.SYSTEM:
            system_parts.append(message.content)
        elif message.role is MessageRole.USER:
            result.append({"role": "user", "content": message.content})
        elif message.role is MessageRole.ASSISTANT:
            blocks: list[dict[str, object]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.arguments_json)
                except json.JSONDecodeError as exc:
                    raise ToolProtocolError("assistant tool arguments must be JSON") from exc
                if not isinstance(arguments, dict):
                    raise ToolProtocolError("assistant tool arguments must be an object")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": arguments,
                    }
                )
            result.append({"role": "assistant", "content": blocks})
        elif message.role is MessageRole.TOOL:
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": model_tool_result_content(message),
                        }
                    ],
                }
            )
    if definitions:
        system_parts.append(tool_result_usage_prompt(definitions))
    return "\n".join(part for part in system_parts if part), result


def _response_from_payload(payload: object, ids: IdGenerator | None) -> LLMResponse:
    body = _mapping(payload)
    content = ""
    calls: list[ToolCall] = []
    for block_raw in _sequence(body.get("content")):
        block = _mapping(block_raw)
        kind = _text(block.get("type"))
        if kind == "text":
            content += _text(block.get("text"))
        elif kind == "tool_use":
            call_id = _text(block.get("id")) or _new_call_id(ids, len(calls))
            calls.append(
                ToolCall(
                    call_id=call_id,
                    name=_text(block.get("name")),
                    arguments_json=json.dumps(block.get("input", {}), separators=(",", ":")),
                )
            )
    return LLMResponse(
        content,
        _usage_from_mapping(_mapping(body.get("usage"))),
        _optional_text(body.get("stop_reason")),
        tuple(calls),
    )


async def _sse_payloads(response: httpx.Response) -> AsyncIterator[dict[str, object]]:
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict):
                    raise LLMError("Anthropic stream payload must be an object")
                yield cast(dict[str, object], payload)
                data = []
            continue
        if line.startswith("data:"):
            data.append(line.removeprefix("data:").lstrip())
    if data:
        payload = json.loads("\n".join(data))
        if not isinstance(payload, dict):
            raise LLMError("Anthropic stream payload must be an object")
        yield cast(dict[str, object], payload)


def _usage_from_mapping(
    payload: dict[str, object],
    *,
    prior: LLMUsage | None = None,
) -> LLMUsage:
    prompt = _optional_integer(payload.get("input_tokens"))
    completion = _optional_integer(payload.get("output_tokens"))
    if prior is not None:
        prompt = max(prompt if prompt is not None else 0, prior.prompt_tokens)
        completion = max(completion if completion is not None else 0, prior.completion_tokens)
    elif prompt is not None and completion is None:
        completion = 0
    if prompt is None or completion is None:
        raise LLMError("Anthropic returned a malformed response")
    return LLMUsage(prompt, completion, prompt + completion)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LLMError("Anthropic returned a malformed response")
    return cast(dict[str, object], value)


def _sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise LLMError("Anthropic returned a malformed response")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise LLMError("Anthropic returned a malformed response")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or value < 0:
        raise LLMError("Anthropic returned a malformed response")
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _index(payload: dict[str, object]) -> int:
    return _integer(payload.get("index"))


def _new_call_id(ids: IdGenerator | None, index: int) -> str:
    return ids.new_id("anthropic_tool") if ids is not None else f"anthropic_tool_{index}"
