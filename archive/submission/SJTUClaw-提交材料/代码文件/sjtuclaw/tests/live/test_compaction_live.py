from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest

from sjtuclaw.config import Settings
from sjtuclaw.entrypoints.cli import CLIState, _handle_command
from sjtuclaw.infrastructure.llm import DeepSeekLLMClient
from sjtuclaw.infrastructure.persistence import JsonMemoryStore, JsonSessionStore
from sjtuclaw.infrastructure.runtime import SystemClock, UuidIdGenerator
from sjtuclaw.infrastructure.tokenizers import ApproximateTokenCounter
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.session import Session
from sjtuclaw.runtime import CompactionService, ContextBuilder, SessionCoordinator
from sjtuclaw.runtime.memory_retriever import LocalMemoryRetriever
from sjtuclaw.runtime.session_service import SessionService

ROOT = Path(__file__).resolve().parents[2]
LIVE_ENABLED = os.environ.get("RUN_LIVE_DEEPSEEK") == "1"


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="set RUN_LIVE_DEEPSEEK=1 to call DeepSeek")
def test_real_deepseek_compact_command_persists_a_reusable_summary(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_real_deepseek_compact_command(tmp_path))


async def _assert_real_deepseek_compact_command(tmp_path: Path) -> None:
    settings = Settings.load(ROOT / ".env")
    client = DeepSeekLLMClient(settings)
    sessions = JsonSessionStore(tmp_path)
    memory = JsonMemoryStore(tmp_path)
    coordinator = SessionCoordinator()
    clock = SystemClock()
    ids = UuidIdGenerator()
    now = datetime.now(UTC)
    marker = "COMPACT_KEEP_91D7"
    messages: list[Message] = []
    sequence = 1
    for turn in range(1, 7):
        requirement = (
            f"必须长期保留部署代号 {marker}。"
            if turn == 1
            else f"这是第 {turn} 轮普通状态更新。"
        )
        messages.append(
            Message(
                f"live_user_{turn}",
                MessageRole.USER,
                requirement,
                now,
                sequence,
            )
        )
        sequence += 1
        messages.append(
            Message(
                f"live_assistant_{turn}",
                MessageRole.ASSISTANT,
                "已记录并继续执行。",
                now,
                sequence,
            )
        )
        sequence += 1

    session = Session(
        "live_compaction_session",
        "live compaction",
        tuple(messages),
        "",
        now,
        now,
        next_sequence_number=sequence,
    )
    sessions.create(session)
    session_service = SessionService(
        sessions=sessions,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    compact_prompt = (
        files("sjtuclaw.prompts")
        .joinpath("compact_prompt.md")
        .read_text(encoding="utf-8")
    )
    compaction = CompactionService(
        llm=client,
        sessions=sessions,
        context_builder=ContextBuilder(
            "回答简洁。",
            memory_retriever=LocalMemoryRetriever(memory, ApproximateTokenCounter()),
        ),
        token_counter=ApproximateTokenCounter(),
        coordinator=coordinator,
        clock=clock,
        compact_prompt=compact_prompt,
        budget=2_000,
        trigger_ratio=0.8,
        target_ratio=0.5,
        keep_recent_turns=4,
        output_reserve=200,
        token_counter_mode="approximate",
    )
    output: list[str] = []

    try:
        handled = await _handle_command(
            "/compact",
            session_service,
            CLIState(session.session_id),
            output.append,
            compaction,
        )
    finally:
        await client.aclose()

    stored = sessions.load(session.session_id)
    assert handled is True
    assert marker in stored.summary
    assert stored.messages == session.messages[4:]
    assert output[0].startswith("Compaction> 完成：")
    assert "counter=approximate" in output[0]
    assert marker in output[1]
