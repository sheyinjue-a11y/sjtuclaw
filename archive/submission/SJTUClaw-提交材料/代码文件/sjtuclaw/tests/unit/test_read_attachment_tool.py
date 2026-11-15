"""Session-scoped attachment reads through trusted tool execution context."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from sjtuclaw.infrastructure.persistence.attachments import JsonAttachmentStore
from sjtuclaw.infrastructure.tools.readonly import create_read_only_tools
from sjtuclaw.infrastructure.tools.registry import ToolRegistry
from sjtuclaw.models.runtime import InvocationSource
from sjtuclaw.models.tool import ToolCall, ToolExecutionContext, mutable_json
from sjtuclaw.runtime.permission import PermissionPolicy

NOW = datetime(2026, 7, 20, tzinfo=UTC)


async def _chunks(value: bytes) -> AsyncIterator[bytes]:
    yield value


def test_read_attachment_uses_trusted_session_context_and_opaque_cursor(tmp_path: Path) -> None:
    asyncio.run(_assert_read_attachment_uses_trusted_session_context_and_opaque_cursor(tmp_path))


async def _assert_read_attachment_uses_trusted_session_context_and_opaque_cursor(
    tmp_path: Path,
) -> None:
    attachments = JsonAttachmentStore(tmp_path)
    await attachments.ingest_text(
        session_id="session_001",
        attachment_id="attachment_001",
        display_name="notes.txt",
        chunks=_chunks(b"abcdefgh"),
        created_at=NOW,
    )
    registry = ToolRegistry(create_read_only_tools(tmp_path, attachments=attachments))
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    call = ToolCall(
        "call_001",
        "read_attachment",
        '{"attachment_id":"attachment_001","max_chars":4}',
    )
    intent = registry.validate_batch((call,))[0]
    assert intent.target_resources == ()
    context = ToolExecutionContext("session_001", "request_001", InvocationSource.CLI)

    first = (await registry.execute_batch((policy.authorize(intent),), context=context))[0]

    assert first.is_error is False
    first_payload = json.loads(first.content)
    assert first_payload["content"] == "abcd"
    assert isinstance(first_payload["next_cursor"], str)
    assert mutable_json(first.structured_content) == {
        "attachment_id": "attachment_001",
        "truncated": True,
        "next_cursor": first_payload["next_cursor"],
    }
    assert first.resources == ()
    assert "session_001" not in first.content

    next_call = ToolCall(
        "call_002",
        "read_attachment",
        json.dumps(
            {
                "attachment_id": "attachment_001",
                "cursor": first_payload["next_cursor"],
                "max_chars": 4,
            }
        ),
    )
    next_intent = registry.validate_batch((next_call,))[0]
    second = (await registry.execute_batch((policy.authorize(next_intent),), context=context))[0]

    assert json.loads(second.content) == {"content": "efgh", "next_cursor": None}
    assert mutable_json(second.structured_content) == {
        "attachment_id": "attachment_001",
        "truncated": False,
        "next_cursor": None,
    }
    assert second.resources == ()

    forged = ToolCall(
        "call_003",
        "read_attachment",
        '{"attachment_id":"attachment_001","cursor":"forged-offset"}',
    )
    forged_intent = registry.validate_batch((forged,))[0]
    forged_result = (
        await registry.execute_batch((policy.authorize(forged_intent),), context=context)
    )[0]

    assert forged_result.is_error is True
    assert forged_result.content == "ATTACHMENT_CURSOR_INVALID"


def test_read_attachment_rejects_cross_session_context_without_storage_path(tmp_path: Path) -> None:
    asyncio.run(_assert_read_attachment_rejects_cross_session_context(tmp_path))


async def _assert_read_attachment_rejects_cross_session_context(tmp_path: Path) -> None:
    attachments = JsonAttachmentStore(tmp_path)
    await attachments.ingest_text(
        session_id="session_001",
        attachment_id="attachment_001",
        display_name="notes.txt",
        chunks=_chunks(b"private"),
        created_at=NOW,
    )
    registry = ToolRegistry(create_read_only_tools(tmp_path, attachments=attachments))
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    intent = registry.validate_batch(
        (ToolCall("call_001", "read_attachment", '{"attachment_id":"attachment_001"}'),)
    )[0]

    result = (
        await registry.execute_batch(
            (policy.authorize(intent),),
            context=ToolExecutionContext("session_002", "request_001", InvocationSource.CLI),
        )
    )[0]

    assert result.is_error is True
    assert result.error_code == "TOOL_EXECUTION_ERROR"
    assert result.content == "ATTACHMENT_SESSION_MISMATCH"
    assert str(tmp_path) not in result.content
