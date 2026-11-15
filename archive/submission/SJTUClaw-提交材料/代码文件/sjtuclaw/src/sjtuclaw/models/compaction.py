"""Provider-independent results for session compaction."""

from dataclasses import dataclass
from enum import StrEnum


class CompactionStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    NO_ELIGIBLE_MESSAGES = "no_eligible_messages"
    COMPACTED = "compacted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CompactionResult:
    status: CompactionStatus
    session_id: str
    before_tokens: int
    after_tokens: int
    compacted_message_count: int
    recent_message_count: int
    summary_preview: str
    target_met: bool
    token_counter_mode: str = "deterministic"
    summary_prompt_tokens: int | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if min(
            self.before_tokens,
            self.after_tokens,
            self.compacted_message_count,
            self.recent_message_count,
        ) < 0:
            raise ValueError("compaction token and message counts must not be negative")
        if self.status is CompactionStatus.FAILED:
            if not self.error_type or not self.error_message:
                raise ValueError("failed compaction results require error details")
        elif self.error_type is not None or self.error_message is not None:
            raise ValueError("successful compaction results must not contain error details")
        if not self.token_counter_mode:
            raise ValueError("token_counter_mode must not be empty")
        if self.summary_prompt_tokens is not None and self.summary_prompt_tokens < 0:
            raise ValueError("summary_prompt_tokens must not be negative")
