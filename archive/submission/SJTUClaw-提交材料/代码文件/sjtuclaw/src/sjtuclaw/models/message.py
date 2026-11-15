"""Conversation message models independent of provider formats."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sjtuclaw.models.tool import (
    JsonValue,
    ToolCall,
    ToolResourceLink,
    ToolResultStatus,
    freeze_json,
)


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class UserInputKind(StrEnum):
    USER = "user"
    STEER = "steer"


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    role: MessageRole
    content: str
    created_at: datetime
    sequence_number: int
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None
    tool_name: str | None = None
    tool_result_status: ToolResultStatus | None = None
    tool_error_code: str | None = None
    tool_structured_content: Mapping[str, JsonValue] | None = None
    tool_resources: tuple[ToolResourceLink, ...] = ()
    attachment_refs: tuple[str, ...] = ()
    input_kind: UserInputKind = UserInputKind.USER

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("message_id must not be empty")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.sequence_number < 0:
            raise ValueError("sequence_number must not be negative")
        if any(not reference for reference in self.attachment_refs):
            raise ValueError("attachment_refs must not contain empty values")
        if len(self.attachment_refs) != len(set(self.attachment_refs)):
            raise ValueError("attachment_refs must be unique")
        if self.attachment_refs and self.role is not MessageRole.USER:
            raise ValueError("attachment_refs are only valid for user messages")
        if self.input_kind is UserInputKind.STEER and self.role is not MessageRole.USER:
            raise ValueError("steer input_kind is only valid for user messages")
        if self.reasoning_content is not None and self.role is not MessageRole.ASSISTANT:
            raise ValueError("reasoning_content is only valid for assistant messages")
        if self.tool_calls and self.role is not MessageRole.ASSISTANT:
            raise ValueError("tool_calls are only valid for assistant messages")
        call_ids = [call.call_id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("tool call IDs must be unique within an assistant message")
        if self.tool_structured_content is not None:
            frozen_content = freeze_json(self.tool_structured_content)
            if not isinstance(frozen_content, Mapping):
                raise TypeError("tool_structured_content must be an object")
            object.__setattr__(self, "tool_structured_content", frozen_content)
        resources = tuple(self.tool_resources)
        object.__setattr__(self, "tool_resources", resources)
        if (
            self.tool_structured_content is not None or resources
        ) and self.role is not MessageRole.TOOL:
            raise ValueError("typed tool result fields are only valid for tool messages")

        result_metadata = (
            self.tool_name,
            self.tool_result_status,
            self.tool_error_code,
        )
        if self.role is MessageRole.TOOL:
            if self.tool_call_id is None:
                raise ValueError("tool messages require tool_call_id")
            if any(value is not None for value in result_metadata):
                if self.tool_name is None or self.tool_result_status is None:
                    raise ValueError("structured tool messages require name and status")
                if (
                    self.tool_result_status is ToolResultStatus.SUCCESS
                    and self.tool_error_code is not None
                ):
                    raise ValueError("successful tool messages cannot have an error code")
                if (
                    self.tool_result_status is ToolResultStatus.ERROR
                    and self.tool_error_code is None
                ):
                    raise ValueError("error tool messages require an error code")
                if (
                    self.tool_result_status is ToolResultStatus.ERROR
                    and resources
                ):
                    raise ValueError("error tool messages cannot have resources")
        elif any(value is not None for value in result_metadata):
            raise ValueError("tool result metadata is only valid for tool messages")
