"""Text-only DeepSeek Chat Completions adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final, cast

import httpx

from sjtuclaw.config import Settings
from sjtuclaw.errors import ConfigurationError, LLMError, ToolProtocolError
from sjtuclaw.infrastructure.llm.tool_protocol import (
    json_messages,
    native_messages,
    native_tool_definitions,
    parse_json_response,
)
from sjtuclaw.models.llm import (
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMToolMode,
    LLMUsage,
)
from sjtuclaw.models.tool import ToolCall
from sjtuclaw.ports.runtime import IdGenerator

Sleep = Callable[[float], Awaitable[None]]

_MAX_RETRIES: Final = 2
_INITIAL_BACKOFF_SECONDS: Final = 0.5
_INSUFFICIENT_SYSTEM_RESOURCE: Final = "insufficient_system_resource"


class _RetryableModelError(Exception):
    """Internal signal for a valid transient model finish reason."""


class DeepSeekLLMClient:
    """Call DeepSeek through its OpenAI-compatible text endpoint."""

    def __init__(
        self,
        settings: Settings,
        *,
        thinking_enabled: bool | None = True,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        ids: IdGenerator | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport cannot both be provided")

        self._settings = settings
        self._owns_client = client is None
        timeout = httpx.Timeout(120.0, connect=10.0)
        self._client = client or httpx.AsyncClient(transport=transport, timeout=timeout)
        self._sleep = sleep
        self._ids = ids
        self._thinking_enabled = thinking_enabled
        self._endpoint = f"{settings.base_url.rstrip('/')}/chat/completions"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        messages = self._serialize_messages(request)
        headers = {"Authorization": f"Bearer {self._settings.api_key}"}
        payload: dict[str, object] = {
            "model": self._settings.model,
            "messages": messages,
            "stream": False,
        }
        if self._thinking_enabled is not None:
            payload["thinking"] = {
                "type": "enabled" if self._thinking_enabled else "disabled",
            }
        if request.tools and request.tool_mode is LLMToolMode.NATIVE:
            payload["tools"] = native_tool_definitions(request.tools)
        elif request.tools and request.tool_mode is LLMToolMode.JSON:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                )
            except httpx.TransportError:
                if attempt == _MAX_RETRIES:
                    raise LLMError(
                        "DeepSeek request failed after retryable transport errors"
                    ) from None
                await self._backoff(attempt)
                continue
            except httpx.HTTPError:
                raise LLMError("DeepSeek request failed before receiving a response") from None

            if _is_retryable_status(response.status_code):
                if attempt == _MAX_RETRIES:
                    raise LLMError(
                        "DeepSeek request failed after retryable "
                        f"HTTP status {response.status_code}"
                    )
                await self._backoff(attempt)
                continue

            if response.status_code != 200:
                raise LLMError(
                    f"DeepSeek request failed with HTTP status {response.status_code}"
                )

            try:
                return _parse_response(response, request, self._ids)
            except _RetryableModelError:
                if attempt == _MAX_RETRIES:
                    raise LLMError(
                        "DeepSeek request failed after retryable model errors"
                    ) from None
                await self._backoff(attempt)

        raise AssertionError("retry loop ended unexpectedly")

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Yield visible answer deltas, then one fully assembled response.

        Reasoning and incomplete native tool fragments stay inside this adapter.
        """

        messages = self._serialize_messages(request)
        headers = {"Authorization": f"Bearer {self._settings.api_key}"}
        payload: dict[str, object] = {
            "model": self._settings.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self._thinking_enabled is not None:
            payload["thinking"] = {
                "type": "enabled" if self._thinking_enabled else "disabled",
            }
        if request.tools and request.tool_mode is LLMToolMode.NATIVE:
            payload["tools"] = native_tool_definitions(request.tools)
        elif request.tools and request.tool_mode is LLMToolMode.JSON:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(_MAX_RETRIES + 1):
            try:
                retryable_status = False
                async with self._client.stream(
                    "POST",
                    self._endpoint,
                    headers=headers,
                    json=payload,
                ) as response:
                    if _is_retryable_status(response.status_code):
                        retryable_status = True
                    elif response.status_code != 200:
                        raise LLMError(
                            f"DeepSeek request failed with HTTP status {response.status_code}"
                        )
                    else:
                        assembler = _StreamAssembler(request, self._ids)
                        async for item in _sse_payloads(response):
                            event = assembler.accept(item)
                            if event is not None:
                                yield event
                        completed = assembler.complete()
                        if (
                            request.tool_mode is LLMToolMode.JSON
                            and not completed.tool_calls
                            and completed.content
                        ):
                            yield LLMStreamEvent(content_delta=completed.content)
                        yield LLMStreamEvent(response=completed)
                        return
            except httpx.TransportError:
                if attempt == _MAX_RETRIES:
                    raise LLMError(
                        "DeepSeek request failed after retryable transport errors"
                    ) from None
                await self._backoff(attempt)
                continue

            if retryable_status:
                if attempt == _MAX_RETRIES:
                    raise LLMError(
                        "DeepSeek request failed after retryable "
                        f"HTTP status {response.status_code}"
                    )
                await self._backoff(attempt)
                continue

        raise AssertionError("retry loop ended unexpectedly")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(_INITIAL_BACKOFF_SECONDS * (2**attempt))

    @staticmethod
    def _serialize_messages(request: LLMRequest) -> list[dict[str, object]]:
        if request.tool_mode is LLMToolMode.JSON:
            return json_messages(request.messages, request.tools)
        return native_messages(request.messages, request.tools)


@dataclass(slots=True)
class _StreamToolCall:
    call_id: str = ""
    kind: str = ""
    name: str = ""
    arguments: str = ""


class _StreamAssembler:
    """Collect provider chunks without exposing internal reasoning or tool fragments."""

    def __init__(self, request: LLMRequest, ids: IdGenerator | None) -> None:
        self._request = request
        self._ids = ids
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._tool_calls: dict[int, _StreamToolCall] = {}
        self._finish_reason: str | None = None
        self._usage: LLMUsage | None = None
        self._emit_visible_deltas = request.tool_mode is not LLMToolMode.JSON

    def accept(self, raw_payload: object) -> LLMStreamEvent | None:
        payload = _require_mapping(raw_payload)
        if "usage" in payload and payload["usage"] is not None:
            self._usage = _usage_from_payload(payload["usage"])

        choices = payload.get("choices")
        if choices == []:
            return None
        if not isinstance(choices, list) or not choices:
            raise LLMError("DeepSeek returned a malformed streaming response")
        if self._finish_reason is not None:
            raise LLMError("DeepSeek returned data after streaming completion")
        choice = _require_mapping(cast(list[object], choices)[0])
        delta = _require_mapping(choice.get("delta"))
        self._append_delta(delta)

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str):
                raise LLMError("DeepSeek returned a malformed streaming response")
            if self._finish_reason is not None:
                raise LLMError("DeepSeek returned multiple streaming completions")
            self._finish_reason = finish_reason

        content = delta.get("content")
        if content is None:
            return None
        if not isinstance(content, str):
            raise LLMError("DeepSeek returned a malformed streaming response")
        if not content or not self._emit_visible_deltas:
            return None
        return LLMStreamEvent(content_delta=content)

    def complete(self) -> LLMResponse:
        if self._finish_reason is None or self._usage is None:
            raise LLMError("DeepSeek stream ended without a complete response")

        tool_calls = self._complete_tool_calls()
        message: dict[str, object] = {
            "role": "assistant",
            "content": "".join(self._content),
        }
        if self._reasoning:
            message["reasoning_content"] = "".join(self._reasoning)
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage_payload: dict[str, object] = {
            "prompt_tokens": self._usage.prompt_tokens,
            "completion_tokens": self._usage.completion_tokens,
            "total_tokens": self._usage.total_tokens,
        }
        if self._usage.prompt_cache_hit_tokens is not None:
            usage_payload["prompt_cache_hit_tokens"] = self._usage.prompt_cache_hit_tokens
            usage_payload["prompt_cache_miss_tokens"] = self._usage.prompt_cache_miss_tokens
        payload: dict[str, object] = {
            "choices": [
                {"message": message, "finish_reason": self._finish_reason},
            ],
            "usage": usage_payload,
        }
        return _parse_payload(payload, self._request, self._ids)

    def _append_delta(self, delta: dict[str, object]) -> None:
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise LLMError("DeepSeek returned a malformed streaming response")
            self._content.append(content)

        reasoning = delta.get("reasoning_content")
        if reasoning is not None:
            if not isinstance(reasoning, str):
                raise LLMError("DeepSeek returned a malformed streaming response")
            self._reasoning.append(reasoning)

        raw_tool_calls = delta.get("tool_calls")
        if raw_tool_calls is None:
            return
        if not isinstance(raw_tool_calls, list):
            raise ToolProtocolError("DeepSeek native tool_calls must be an array")
        for raw_call in cast(list[object], raw_tool_calls):
            call = _require_tool_mapping(raw_call)
            index = call.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ToolProtocolError("DeepSeek native tool call index must be non-negative")
            fragment = self._tool_calls.setdefault(index, _StreamToolCall())
            fragment.call_id += _optional_fragment(call, "id")
            fragment.kind += _optional_fragment(call, "type")
            function = call.get("function")
            if function is not None:
                function_data = _require_tool_mapping(function)
                fragment.name += _optional_fragment(function_data, "name")
                fragment.arguments += _optional_fragment(function_data, "arguments")

    def _complete_tool_calls(self) -> list[dict[str, object]]:
        if not self._tool_calls:
            return []
        indexes = sorted(self._tool_calls)
        if indexes != list(range(len(indexes))):
            raise ToolProtocolError("DeepSeek native tool call indexes are not contiguous")
        return [
            {
                "id": self._tool_calls[index].call_id,
                "type": self._tool_calls[index].kind,
                "function": {
                    "name": self._tool_calls[index].name,
                    "arguments": self._tool_calls[index].arguments,
                },
            }
            for index in indexes
        ]


async def _sse_payloads(response: httpx.Response) -> AsyncIterator[object]:
    saw_done = False
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            saw_done = True
            break
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            raise LLMError("DeepSeek returned a malformed streaming response") from None
    if not saw_done:
        raise LLMError("DeepSeek stream ended without [DONE]")


def _optional_fragment(value: dict[str, object], field: str) -> str:
    item = value.get(field)
    if item is None:
        return ""
    if not isinstance(item, str):
        raise ToolProtocolError(f"DeepSeek native tool call {field} must be text")
    return item


def _parse_response(
    response: httpx.Response,
    request: LLMRequest,
    ids: IdGenerator | None,
) -> LLMResponse:
    try:
        raw_payload: object = response.json()
    except ValueError:
        raise LLMError("DeepSeek returned a malformed response") from None

    return _parse_payload(raw_payload, request, ids)


def _parse_payload(
    raw_payload: object,
    request: LLMRequest,
    ids: IdGenerator | None,
) -> LLMResponse:

    payload = _require_mapping(raw_payload)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("DeepSeek returned a malformed response")
    choice = _require_mapping(cast(list[object], choices)[0])

    if "finish_reason" not in choice:
        raise LLMError("DeepSeek returned a malformed response")
    finish_reason = choice["finish_reason"]
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise LLMError("DeepSeek returned a malformed response")
    if finish_reason == _INSUFFICIENT_SYSTEM_RESOURCE:
        raise _RetryableModelError

    message = _require_mapping(choice.get("message"))
    if message.get("role") != "assistant":
        raise LLMError("DeepSeek returned a malformed response")
    reasoning_content = _optional_text(message.get("reasoning_content"))

    tool_calls: tuple[ToolCall, ...] = ()
    content_value = message.get("content")
    if request.tools and request.tool_mode is LLMToolMode.NATIVE:
        tool_calls = _parse_native_tool_calls(message.get("tool_calls"))
        if tool_calls:
            if finish_reason != "tool_calls" or content_value is not None and not isinstance(
                content_value, str
            ):
                raise ToolProtocolError("DeepSeek returned inconsistent native tool calls")
            content = "" if content_value is None else content_value
        else:
            if finish_reason == "tool_calls" or not isinstance(content_value, str):
                raise ToolProtocolError("DeepSeek returned inconsistent native tool calls")
            content = content_value
    elif request.tools and request.tool_mode is LLMToolMode.JSON:
        if not isinstance(content_value, str):
            raise ToolProtocolError("DeepSeek returned non-text JSON tool protocol")
        if ids is None:
            raise ConfigurationError("JSON tool mode requires an IdGenerator")
        content, tool_calls = parse_json_response(content_value, ids)
    else:
        if message.get("tool_calls") not in (None, []):
            raise LLMError("DeepSeek returned unexpected tool calls")
        if not isinstance(content_value, str):
            raise LLMError("DeepSeek returned a malformed response")
        content = content_value

    usage = _usage_from_payload(payload.get("usage"))
    try:
        return LLMResponse(
            content=content,
            usage=usage,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )
    except (TypeError, ValueError) as exc:
        raise ToolProtocolError("DeepSeek returned invalid tool call identifiers") from exc


def _parse_native_tool_calls(value: object) -> tuple[ToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ToolProtocolError("DeepSeek native tool_calls must be an array")
    calls: list[ToolCall] = []
    for raw_call in cast(list[object], value):
        call = _require_tool_mapping(raw_call)
        if call.get("type") != "function":
            raise ToolProtocolError("DeepSeek native tool call type must be function")
        function = _require_tool_mapping(call.get("function"))
        call_id = call.get("id")
        name = function.get("name")
        arguments = function.get("arguments")
        if not all(isinstance(item, str) for item in (call_id, name, arguments)):
            raise ToolProtocolError("DeepSeek native tool call fields must be text")
        try:
            calls.append(
                ToolCall(
                    cast(str, call_id),
                    cast(str, name),
                    cast(str, arguments),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ToolProtocolError("DeepSeek native tool call is invalid") from exc
    return tuple(calls)


def _require_tool_mapping(value: object) -> dict[str, object]:
    """Map malformed native tool-call structures to the tool protocol taxonomy."""

    if not isinstance(value, dict):
        raise ToolProtocolError("DeepSeek native tool call must be an object")
    return cast(dict[str, object], value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LLMError("DeepSeek returned a malformed response")
    return value


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _require_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LLMError("DeepSeek returned a malformed response")
    return cast(dict[str, object], value)


def _require_token_count(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LLMError("DeepSeek returned a malformed response")
    return value


def _usage_from_payload(value: object) -> LLMUsage:
    payload = _require_mapping(value)
    prompt_tokens = _require_token_count(payload, "prompt_tokens")
    hit_present = "prompt_cache_hit_tokens" in payload
    miss_present = "prompt_cache_miss_tokens" in payload
    if hit_present != miss_present:
        raise LLMError("DeepSeek returned a malformed response")
    hit_tokens = (
        _require_token_count(payload, "prompt_cache_hit_tokens")
        if hit_present
        else None
    )
    miss_tokens = (
        _require_token_count(payload, "prompt_cache_miss_tokens")
        if miss_present
        else None
    )
    try:
        return LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=_require_token_count(payload, "completion_tokens"),
            total_tokens=_require_token_count(payload, "total_tokens"),
            prompt_cache_hit_tokens=hit_tokens,
            prompt_cache_miss_tokens=miss_tokens,
        )
    except ValueError as exc:
        raise LLMError("DeepSeek returned a malformed response") from exc
