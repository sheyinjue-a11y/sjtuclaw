"""Explicit UTF-8 size approximation for environments without a model tokenizer."""

import json
from math import ceil

from sjtuclaw.infrastructure.llm.tool_protocol import (
    json_messages,
    native_messages,
    native_tool_definitions,
)
from sjtuclaw.models.llm import LLMRequest, LLMToolMode


class ApproximateTokenCounter:
    """Estimate one token per four UTF-8 bytes; never used as an implicit fallback."""

    def count(self, request: LLMRequest) -> int:
        messages = (
            json_messages(request.messages, request.tools)
            if request.tool_mode is LLMToolMode.JSON
            else native_messages(request.messages, request.tools)
        )
        payload: dict[str, object] = {"messages": messages}
        if request.tool_mode is LLMToolMode.NATIVE:
            payload["tools"] = native_tool_definitions(request.tools)
        elif request.tool_mode is LLMToolMode.JSON:
            payload["response_format"] = {"type": "json_object"}
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return max(1, ceil(len(serialized) / 4))
