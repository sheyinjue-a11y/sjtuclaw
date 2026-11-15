from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sjtuclaw.models.llm import LLMResponse, LLMUsage
from sjtuclaw.models.message import MessageRole
from sjtuclaw.runtime.session_title import SessionTitleGenerator
from tests.fakes import FakeClock, FakeLLMClient

NOW = datetime(2026, 7, 25, tzinfo=UTC)


class NeverRespondingLLM:
    async def complete(self, request: object) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_session_title_generator_uses_first_round_without_tools() -> None:
    asyncio.run(_assert_session_title_generator_uses_first_round_without_tools())


async def _assert_session_title_generator_uses_first_round_without_tools() -> None:
    usage = LLMUsage(3, 2, 5)
    llm = FakeLLMClient([LLMResponse("课程重点整理方法", usage, "stop")])
    generator = SessionTitleGenerator(
        llm=llm,
        clock=FakeClock(NOW),
        timeout_seconds=1,
    )

    result = await generator.generate(
        "如何整理课程重点",
        "按章节归纳并制作复习清单。",
    )

    assert result.title == "课程重点整理方法"
    assert result.usage == usage
    request = llm.requests[0]
    assert request.tools == ()
    assert [message.role for message in request.messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
    ]
    assert "如何整理课程重点" in request.messages[-1].content
    assert "按章节归纳并制作复习清单。" in request.messages[-1].content


def test_session_title_generator_cleans_model_formatting() -> None:
    asyncio.run(_assert_session_title_generator_cleans_model_formatting())


async def _assert_session_title_generator_cleans_model_formatting() -> None:
    usage = LLMUsage(2, 2, 4)
    llm = FakeLLMClient([LLMResponse("  “标题：课程\n复习计划”  ", usage, "stop")])
    generator = SessionTitleGenerator(llm=llm, clock=FakeClock(NOW))

    result = await generator.generate("课程怎么复习", "制定分阶段计划。")

    assert result.title == "课程 复习计划"
    assert result.usage == usage


def test_session_title_generator_truncates_to_configured_limit() -> None:
    asyncio.run(_assert_session_title_generator_truncates_to_configured_limit())


async def _assert_session_title_generator_truncates_to_configured_limit() -> None:
    usage = LLMUsage(2, 2, 4)
    llm = FakeLLMClient([LLMResponse("abcdefghijklmnopqrstu", usage, "stop")])
    generator = SessionTitleGenerator(
        llm=llm,
        clock=FakeClock(NOW),
        max_chars=20,
    )

    result = await generator.generate("fallback", "answer")

    assert result.title == "abcdefghijklmnopqrs…"


def test_session_title_generator_falls_back_for_empty_model_output() -> None:
    asyncio.run(_assert_session_title_generator_falls_back_for_empty_model_output())


async def _assert_session_title_generator_falls_back_for_empty_model_output() -> None:
    usage = LLMUsage(2, 1, 3)
    llm = FakeLLMClient([LLMResponse("  标题：  ", usage, "stop")])
    generator = SessionTitleGenerator(llm=llm, clock=FakeClock(NOW))

    result = await generator.generate("  帮我   整理课程复习重点  ", "回答")

    assert result.title == "帮我 整理课程复习重点"
    assert result.usage == usage


def test_session_title_generator_falls_back_when_provider_fails() -> None:
    asyncio.run(_assert_session_title_generator_falls_back_when_provider_fails())


async def _assert_session_title_generator_falls_back_when_provider_fails() -> None:
    generator = SessionTitleGenerator(
        llm=FakeLLMClient([]),
        clock=FakeClock(NOW),
    )

    result = await generator.generate("首轮问题", "首轮回答")

    assert result.title == "首轮问题"
    assert result.usage is None


def test_session_title_generator_times_out_to_fallback() -> None:
    asyncio.run(_assert_session_title_generator_times_out_to_fallback())


async def _assert_session_title_generator_times_out_to_fallback() -> None:
    generator = SessionTitleGenerator(
        llm=NeverRespondingLLM(),  # type: ignore[arg-type]
        clock=FakeClock(NOW),
        timeout_seconds=0.01,
    )

    result = await asyncio.wait_for(
        generator.generate("超时回退问题", "不会用到"),
        timeout=0.1,
    )

    assert result.title == "超时回退问题"
    assert result.usage is None
