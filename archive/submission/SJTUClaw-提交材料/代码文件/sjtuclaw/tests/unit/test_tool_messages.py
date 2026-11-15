from datetime import UTC, datetime

import pytest

from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.tool import (
    JsonValue,
    ToolCall,
    ToolResourceLink,
    ToolResultStatus,
    mutable_json,
)

NOW = datetime(2026, 7, 15, tzinfo=UTC)


def test_assistant_tool_message_preserves_calls_and_reasoning() -> None:
    call = ToolCall("call_001", "read_file", '{"path":"README.md"}')

    message = Message(
        "message_001",
        MessageRole.ASSISTANT,
        "",
        NOW,
        1,
        tool_calls=(call,),
        reasoning_content="private model reasoning",
    )

    assert message.tool_calls == (call,)
    assert message.reasoning_content == "private model reasoning"


def test_structured_tool_result_requires_consistent_status_metadata() -> None:
    message = Message(
        "message_002",
        MessageRole.TOOL,
        "safe observation",
        NOW,
        2,
        tool_call_id="call_001",
        tool_name="read_file",
        tool_result_status=ToolResultStatus.ERROR,
        tool_error_code="FILE_NOT_FOUND",
    )

    assert message.tool_result_status is ToolResultStatus.ERROR
    with pytest.raises(ValueError, match="require an error code"):
        Message(
            "message_003",
            MessageRole.TOOL,
            "failed",
            NOW,
            3,
            tool_call_id="call_002",
            tool_name="read_file",
            tool_result_status=ToolResultStatus.ERROR,
        )


def test_tool_message_preserves_typed_content_and_resources() -> None:
    resource = ToolResourceLink(
        "download",
        "report.md",
        "/downloads/download_123",
        "text/markdown",
        "2026-07-31T00:00:00+00:00",
    )
    structured_content: dict[str, JsonValue] = {
        "filename": "report.md",
        "size_bytes": 12,
    }

    message = Message(
        "message_004",
        MessageRole.TOOL,
        "下载快照已创建。",
        NOW,
        4,
        tool_call_id="call_004",
        tool_name="create_download",
        tool_result_status=ToolResultStatus.SUCCESS,
        tool_structured_content=structured_content,
        tool_resources=(resource,),
    )
    structured_content["filename"] = "changed.md"

    assert message.tool_structured_content is not None
    assert mutable_json(message.tool_structured_content) == {
        "filename": "report.md",
        "size_bytes": 12,
    }
    assert message.tool_resources == (resource,)


def test_typed_tool_result_fields_are_rejected_on_non_tool_roles() -> None:
    with pytest.raises(ValueError, match="only valid for tool messages"):
        Message(
            "message_005",
            MessageRole.USER,
            "hello",
            NOW,
            5,
            tool_structured_content={"value": "hidden"},
        )
    with pytest.raises(ValueError, match="only valid for tool messages"):
        Message(
            "message_006",
            MessageRole.USER,
            "hello",
            NOW,
            6,
            tool_resources=(
                ToolResourceLink(
                    "download",
                    "report.md",
                    "/downloads/download_123",
                ),
            ),
        )


def test_error_tool_message_rejects_resources() -> None:
    resource = ToolResourceLink(
        "download",
        "report.md",
        "/downloads/download_123",
    )

    with pytest.raises(ValueError, match="resources"):
        Message(
            "message_007",
            MessageRole.TOOL,
            "download failed",
            NOW,
            6,
            tool_call_id="call_007",
            tool_name="create_download",
            tool_result_status=ToolResultStatus.ERROR,
            tool_error_code="DOWNLOAD_FAILED",
            tool_resources=(resource,),
        )


def test_tool_fields_are_rejected_on_wrong_roles_and_duplicate_calls() -> None:
    call = ToolCall("call_001", "current_time", "{}")
    with pytest.raises(ValueError, match="only valid for assistant"):
        Message(
            "message_001",
            MessageRole.USER,
            "hello",
            NOW,
            1,
            reasoning_content="hidden",
        )
    with pytest.raises(ValueError, match="unique"):
        Message(
            "message_002",
            MessageRole.ASSISTANT,
            "",
            NOW,
            2,
            tool_calls=(call, call),
        )
