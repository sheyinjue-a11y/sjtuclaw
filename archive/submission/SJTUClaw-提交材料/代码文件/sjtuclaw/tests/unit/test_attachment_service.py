"""Attachment lifecycle rules that depend on Session references."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sjtuclaw.errors import AttachmentError
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.infrastructure.persistence.attachments import JsonAttachmentStore
from sjtuclaw.models.llm import LLMResponse, LLMUsage
from sjtuclaw.models.runtime import InvocationSource, RuntimeRequest, UserInput
from sjtuclaw.models.session import (
    PendingRequest,
    RunState,
    RunStatus,
    Session,
    SessionArchiveReason,
)
from sjtuclaw.runtime.attachment_service import AttachmentService
from sjtuclaw.runtime.context_builder import ContextBuilder
from sjtuclaw.runtime.service import RuntimeHost
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from tests.fakes import FakeClock, FakeIdGenerator, FakeLLMClient

NOW = datetime(2026, 7, 20, tzinfo=UTC)


async def _chunks() -> AsyncIterator[bytes]:
    yield b"safe text"


def test_attachment_service_refuses_to_delete_a_pending_reference(tmp_path: Path) -> None:
    sessions = InMemorySessionStore()
    sessions.create(
        Session(
            "session_001",
            "测试",
            (),
            "",
            NOW,
            NOW,
            pending_requests=(
                PendingRequest(
                    "request_001",
                    1,
                    InvocationSource.CLI,
                    None,
                    "问题",
                    NOW,
                    ("attachment_001",),
                ),
            ),
            runs=(RunState("run_001", "request_001", RunStatus.QUEUED),),
        )
    )
    store = JsonAttachmentStore(tmp_path)
    asyncio.run(
        store.ingest_text(
            session_id="session_001",
            attachment_id="attachment_001",
            display_name="notes.txt",
            chunks=_chunks(),
            created_at=NOW,
        )
    )
    service = AttachmentService(
        sessions=sessions,
        attachments=store,
        coordinator=SessionCoordinator(),
    )

    with pytest.raises(AttachmentError, match="ATTACHMENT_IN_USE"):
        asyncio.run(service.delete("session_001", "attachment_001"))

    assert store.load("session_001", "attachment_001").attachment_id == "attachment_001"


def test_attachment_delete_and_submit_share_one_admission_boundary(tmp_path: Path) -> None:
    asyncio.run(_assert_attachment_delete_and_submit_share_one_admission_boundary(tmp_path))


async def _assert_attachment_delete_and_submit_share_one_admission_boundary(
    tmp_path: Path,
) -> None:
    sessions = InMemorySessionStore()
    sessions.create(Session("session_001", "测试", (), "", NOW, NOW))
    attachments = JsonAttachmentStore(tmp_path)
    await attachments.ingest_text(
        session_id="session_001",
        attachment_id="attachment_001",
        display_name="notes.txt",
        chunks=_chunks(),
        created_at=NOW,
    )
    coordinator = SessionCoordinator()
    service = AttachmentService(
        sessions=sessions,
        attachments=attachments,
        coordinator=coordinator,
    )
    host = RuntimeHost(
        llm=FakeLLMClient([LLMResponse("答复", LLMUsage(1, 1, 2), "stop")]),
        sessions=sessions,
        context_builder=ContextBuilder("system"),
        coordinator=coordinator,
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
        attachments=attachments,
    )
    request = RuntimeRequest(
        "request_001",
        "session_001",
        InvocationSource.CLI,
        UserInput("问题"),
        attachment_refs=("attachment_001",),
    )

    await service.delete("session_001", "attachment_001")
    with pytest.raises(AttachmentError, match="ATTACHMENT_NOT_FOUND"):
        await host.submit(request)

    await host.aclose()


def test_attachment_service_rejects_archived_session_mutations(tmp_path: Path) -> None:
    async def scenario() -> None:
        sessions = InMemorySessionStore()
        archived = replace(
            Session("session_001", "archived", (), "", NOW, NOW),
            archived_at=NOW,
            archived_reason=SessionArchiveReason.USER_ARCHIVED,
        )
        sessions.create(archived)
        store = JsonAttachmentStore(tmp_path)
        service = AttachmentService(
            sessions=sessions,
            attachments=store,
            coordinator=SessionCoordinator(),
        )

        with pytest.raises(Exception, match="SESSION_ARCHIVED"):
            await service.ingest_text(
                session_id=archived.session_id,
                attachment_id="attachment_001",
                display_name="notes.txt",
                chunks=_chunks(),
                created_at=NOW,
            )

        assert store.list(archived.session_id) == ()

    asyncio.run(scenario())
