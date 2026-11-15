"""Persistent sanitized audit records independent of transport events."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RunRecordType(StrEnum):
    PERMISSION = "permission"
    ERROR = "error"
    STATUS = "status"


@dataclass(frozen=True, slots=True)
class RunRecord:
    record_id: str
    invocation_id: str
    record_type: RunRecordType
    code: str
    message: str
    created_at: datetime
    sequence_number: int
    tool_call_id: str | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        if not self.record_id or not self.invocation_id:
            raise ValueError("run record identifiers must not be empty")
        if not self.code or not self.message:
            raise ValueError("run record code and message must not be empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("run record timestamp must be timezone-aware")
        if self.sequence_number < 1:
            raise ValueError("run record sequence_number must be positive")
        if (self.tool_call_id is None) != (self.tool_name is None):
            raise ValueError("run record tool fields must be supplied together")
