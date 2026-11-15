from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sjtuclaw.bootstrap import build_cli_application
from sjtuclaw.config import Settings
from sjtuclaw.errors import LLMError
from sjtuclaw.infrastructure.llm.deepseek import DeepSeekLLMClient
from sjtuclaw.models.llm import LLMRequest
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.runtime import (
    InvocationSource,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRequest,
    UserInput,
)
from sjtuclaw.runtime import RunHandle

ROOT = Path(__file__).resolve().parents[2]
LIVE_ENABLED = os.environ.get("RUN_LIVE_DEEPSEEK") == "1"


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="set RUN_LIVE_DEEPSEEK=1 to call DeepSeek")
def test_real_deepseek_basic_and_two_turn_chat_reports_usage() -> None:
    asyncio.run(_assert_real_deepseek_basic_and_two_turn_chat_reports_usage())


async def _assert_real_deepseek_basic_and_two_turn_chat_reports_usage() -> None:
    settings = Settings.load(ROOT / ".env")
    client = DeepSeekLLMClient(settings)
    now = datetime.now(UTC)
    marker = "SJTUCLAW_LIVE_7F2C9A"
    first_messages = (
        Message("system_live", MessageRole.SYSTEM, "回答简洁。", now, 0),
        Message(
            "user_live_1",
            MessageRole.USER,
            f"请记住代号 {marker}。只回复“已记住”，不要复述代号。",
            now,
            1,
        ),
    )

    try:
        first = await client.complete(LLMRequest(first_messages))
        second_messages = (
            *first_messages,
            Message("assistant_live_1", MessageRole.ASSISTANT, first.content, now, 2),
            Message(
                "user_live_2",
                MessageRole.USER,
                "只回复我上一条消息要求你记住的代号，不要添加其他内容。",
                now,
                3,
            ),
        )
        second = await client.complete(LLMRequest(second_messages))
    finally:
        await client.aclose()

    assert first.content.strip()
    assert marker in second.content
    assert first.usage.prompt_tokens > 0
    assert second.usage.prompt_tokens > first.usage.prompt_tokens


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="set RUN_LIVE_DEEPSEEK=1 to call DeepSeek")
def test_real_deepseek_invalid_key_maps_to_safe_llm_error() -> None:
    asyncio.run(_assert_real_deepseek_invalid_key_maps_safely())


async def _assert_real_deepseek_invalid_key_maps_safely() -> None:
    invalid_key = "sjtuclaw-invalid-live-key"
    settings = replace(Settings.load(ROOT / ".env"), api_key=invalid_key)
    client = DeepSeekLLMClient(settings)
    now = datetime.now(UTC)
    request = LLMRequest(
        (
            Message("system_live_error", MessageRole.SYSTEM, "Reply briefly.", now, 0),
            Message("user_live_error", MessageRole.USER, "hello", now, 1),
        )
    )

    try:
        with pytest.raises(LLMError, match="HTTP status") as caught:
            await client.complete(request)
    finally:
        await client.aclose()

    assert invalid_key not in str(caught.value)


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="set RUN_LIVE_DEEPSEEK=1 to call DeepSeek")
@pytest.mark.parametrize("tool_mode", ["native", "json"])
def test_real_deepseek_tool_loop_persists_native_and_json_traces(
    tmp_path: Path,
    tool_mode: str,
) -> None:
    asyncio.run(_assert_real_deepseek_tool_loop(tmp_path, tool_mode))


async def _assert_real_deepseek_tool_loop(tmp_path: Path, tool_mode: str) -> None:
    marker = f"SJTUCLAW_TOOL_{tool_mode.upper()}_4B91"
    (tmp_path / "fixture.txt").write_text(f"marker={marker}\n", encoding="utf-8")
    settings = replace(
        Settings.load(ROOT / ".env"),
        data_dir=tmp_path / "data",
        tool_root=tmp_path,
        tool_mode=tool_mode,  # type: ignore[arg-type]
        token_counter_mode="approximate",
    )
    application = build_cli_application(
        settings,
        "You are a precise assistant. Follow tool instructions before answering.",
        "",
        "Summarize old complete conversation turns.",
    )
    request = RuntimeRequest(
        request_id=f"live_{tool_mode}_request",
        session_id=application.initial_session_id,
        source=InvocationSource.CLI,
        user_input=UserInput(
            "必须先调用 read_file 读取 fixture.txt。然后只回复文件中的 marker 值，不要解释。"
        ),
    )
    try:
        events = await _collect(await application.runtime.submit(request))
    finally:
        await application.aclose()

    tool_call_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type is RuntimeEventType.TOOL_CALL
        and event.payload["name"] == "read_file"
    )
    tool_result_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type is RuntimeEventType.TOOL_RESULT
        and event.payload["tool"] == "read_file"
    )
    final_event = next(
        event for event in events if event.event_type is RuntimeEventType.ASSISTANT_MESSAGE
    )
    stored = application.session_service.load(application.initial_session_id)

    assert tool_call_index < tool_result_index
    assert marker in str(final_event.payload["content"])
    assert any(message.tool_calls for message in stored.messages)
    assert any(
        message.role is MessageRole.TOOL and message.tool_name == "read_file"
        for message in stored.messages
    )
    assert stored.run_records[-1].code == "RUN_COMPLETED"


async def _collect(handle: RunHandle) -> list[RuntimeEvent]:
    events: list[RuntimeEvent] = []
    async for event in handle.events():
        events.append(event)
    return events
