from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sjtuclaw.errors import ConfigurationError
from sjtuclaw.infrastructure.tokenizers import DeepSeekTokenCounter
from sjtuclaw.models.llm import LLMRequest, LLMToolMode
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.tool import ToolDefinition, ToolSafetyLevel

NOW = datetime(2026, 7, 15, tzinfo=UTC)


class FakeTokenizer:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def encode(self, text: str) -> list[int]:
        self.prompts.append(text)
        return [1, 2, 3, 4]


def test_deepseek_counter_uses_official_message_encoder_in_thinking_mode() -> None:
    tokenizer = FakeTokenizer()
    calls: list[tuple[list[dict[str, object]], str, bool]] = []

    def encode_messages(
        messages: list[dict[str, object]],
        *,
        thinking_mode: str,
        drop_thinking: bool,
    ) -> str:
        calls.append((messages, thinking_mode, drop_thinking))
        return "official encoded prompt"

    counter = DeepSeekTokenCounter(
        Path("unused"),
        tokenizer=tokenizer,
        encode_messages=encode_messages,
    )
    request = LLMRequest(
        (
            Message("system", MessageRole.SYSTEM, "rules", NOW, 0),
            Message("user", MessageRole.USER, "hello", NOW, 1),
        )
    )

    assert counter.count(request) == 4
    assert calls == [
        (
            [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "hello"},
            ],
            "thinking",
            True,
        )
    ]
    assert tokenizer.prompts == ["official encoded prompt"]


def test_deepseek_counter_missing_artifacts_fails_without_fallback(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="artifacts missing"):
        DeepSeekTokenCounter(tmp_path)


def test_deepseek_counter_requires_both_injected_components() -> None:
    with pytest.raises(ValueError, match="injected together"):
        DeepSeekTokenCounter(Path("unused"), tokenizer=FakeTokenizer())


def test_deepseek_counter_maps_runtime_encoder_failure_to_configuration_error() -> None:
    def failing_encoder(
        messages: list[dict[str, object]],
        *,
        thinking_mode: str,
        drop_thinking: bool,
    ) -> str:
        raise RuntimeError("encoder internals")

    counter = DeepSeekTokenCounter(
        Path("unused"),
        tokenizer=FakeTokenizer(),
        encode_messages=failing_encoder,
    )
    request = LLMRequest((Message("user", MessageRole.USER, "hello", NOW, 1),))

    with pytest.raises(ConfigurationError, match="failed to count"):
        counter.count(request)


def test_deepseek_counter_attaches_native_tools_to_official_system_message() -> None:
    tokenizer = FakeTokenizer()
    captured: list[list[dict[str, object]]] = []

    def encode_messages(
        messages: list[dict[str, object]],
        *,
        thinking_mode: str,
        drop_thinking: bool,
    ) -> str:
        captured.append(messages)
        return "encoded"

    definition = ToolDefinition(
        name="current_time",
        description="Return current time.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object", "properties": {}, "additionalProperties": False},
        result_usage="Use the current time.",
        safety_level=ToolSafetyLevel.READ_ONLY,
    )
    counter = DeepSeekTokenCounter(
        Path("unused"),
        tokenizer=tokenizer,
        encode_messages=encode_messages,
    )
    request = LLMRequest(
        (Message("system", MessageRole.SYSTEM, "rules", NOW, 0),),
        (definition,),
        LLMToolMode.NATIVE,
    )

    assert counter.count(request) == 4
    assert captured[0][0]["tools"] == [
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
    assert captured[0][1] == {
        "role": "system",
        "content": (
            "Tool result usage rules: content is for explanation; "
            "structured_content is the only machine-readable result field. "
            "Host renders actionable resources. The model must not construct, "
            "guess, or rewrite download links and must not treat internal IDs as URLs. "
            "Filesystem paths are not URLs. Only tool-returned, model-visible fields "
            "explicitly marked as authoritative URLs may be used verbatim. When a "
            "result is truncated, the model must not claim that it received complete "
            "content. Per-tool result usage: current_time: Use the current time."
        ),
    }
    assert "output_schema" not in str(captured[0])
