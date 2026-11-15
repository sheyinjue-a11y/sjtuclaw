import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from sjtuclaw.infrastructure.persistence.attachments import JsonAttachmentStore
from sjtuclaw.models.memory import MemoryEntry
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.session import Session
from sjtuclaw.runtime.context_builder import ContextBuilder
from sjtuclaw.runtime.prompt_configuration import PromptConfigurationService


def test_context_builder_prepends_system_prompt_without_persisting_it() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    user = Message("message_001", MessageRole.USER, "hello", now, 1)
    session = Session("session_001", "test", (user,), "", now, now, 2)

    context = ContextBuilder("system rules").build(session)

    assert [message.role for message in context] == [MessageRole.SYSTEM, MessageRole.USER]
    assert context[0].content == "system rules"
    assert session.messages == (user,)


def test_context_builder_injects_soul_into_one_system_prompt(tmp_path: Path) -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)

    class RecordingRetriever:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def retrieve(self, query: str) -> tuple[MemoryEntry, ...]:
            self.queries.append(query)
            return (MemoryEntry("memory_001", "likes concise answers", now),)

    user = Message("message_001", MessageRole.USER, "hello", now, 1)
    session = Session("session_001", "test", (user,), "earlier summary", now, now, 2)
    retriever = RecordingRetriever()
    prompts = PromptConfigurationService(
        root=tmp_path / "prompts",
        default_system_prompt="system rules",
        default_soul="be kind",
    )
    builder = ContextBuilder(
        prompt_configuration=prompts,
        memory_retriever=retriever,
    )

    context = builder.build(session)

    assert [message.message_id for message in context] == [
        "stable_system_prompt",
        "stable_memory",
        "session_summary",
        "message_001",
    ]
    assert context[0].content == "system rules\n\nSoul:\nbe kind"
    assert "likes concise answers" in context[1].content
    assert retriever.queries == ["hello"]
    assert context[2].content == "Session summary:\nearlier summary"
    assert session.messages == (user,)


def test_context_builder_uses_last_user_message_including_steer_as_memory_query() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)

    class RecordingRetriever:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def retrieve(self, query: str) -> tuple[MemoryEntry, ...]:
            self.queries.append(query)
            return ()

    session = Session(
        "session_001",
        "test",
        (
            Message("message_001", MessageRole.USER, "first", now, 1),
            Message("message_002", MessageRole.ASSISTANT, "answer", now, 2),
            Message("message_003", MessageRole.USER, "change direction", now, 3),
        ),
        "",
        now,
        now,
        4,
    )
    retriever = RecordingRetriever()

    ContextBuilder("system rules", memory_retriever=retriever).build(session)

    assert retriever.queries == ["change direction"]


def test_context_builder_uses_empty_query_without_user_message() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)

    class RecordingRetriever:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def retrieve(self, query: str) -> tuple[MemoryEntry, ...]:
            self.queries.append(query)
            return ()

    session = Session(
        "session_001",
        "test",
        (Message("message_001", MessageRole.ASSISTANT, "answer", now, 1),),
        "",
        now,
        now,
        2,
    )
    retriever = RecordingRetriever()

    ContextBuilder("system rules", memory_retriever=retriever).build(session)

    assert retriever.queries == [""]


async def _chunks(value: bytes) -> AsyncIterator[bytes]:
    yield value


def test_context_builder_injects_attachment_metadata_not_attachment_text(tmp_path: Path) -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    attachments = JsonAttachmentStore(tmp_path)
    asyncio.run(
        attachments.ingest_text(
            session_id="session_001",
            attachment_id="attachment_001",
            display_name="notes.txt",
            chunks=_chunks(b"private attachment body"),
            created_at=now,
        )
    )
    user = Message(
        "message_001",
        MessageRole.USER,
        "please inspect this",
        now,
        1,
        attachment_refs=("attachment_001",),
    )
    session = Session("session_001", "test", (user,), "", now, now, 2)

    context = ContextBuilder("system rules", attachments=attachments).build(session)

    assert [message.role for message in context] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.SYSTEM,
    ]
    metadata = context[-1].content
    assert "attachment_001" in metadata
    assert "notes.txt" in metadata
    assert "read_attachment" in metadata
    assert "private attachment body" not in metadata
