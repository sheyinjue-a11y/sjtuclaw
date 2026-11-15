"""Provider-independent language model request and response data."""

from dataclasses import dataclass
from enum import StrEnum

from sjtuclaw.models.message import Message
from sjtuclaw.models.tool import ToolCall, ToolDefinition


class LLMToolMode(StrEnum):
    NATIVE = "native"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None

    def __post_init__(self) -> None:
        if min(self.prompt_tokens, self.completion_tokens, self.total_tokens) < 0:
            raise ValueError("LLM usage token counts must not be negative")
        cache_counts = (self.prompt_cache_hit_tokens, self.prompt_cache_miss_tokens)
        if any(value is None for value in cache_counts):
            if any(value is not None for value in cache_counts):
                raise ValueError("LLM cache token counts must be supplied together")
            return
        if min(value for value in cache_counts if value is not None) < 0:
            raise ValueError("LLM cache token counts must not be negative")
        if sum(value for value in cache_counts if value is not None) != self.prompt_tokens:
            raise ValueError("LLM cache token counts must equal prompt_tokens")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    tool_mode: LLMToolMode | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("LLM request must contain at least one message")
        if bool(self.tools) != (self.tool_mode is not None):
            raise ValueError("LLM tools and tool_mode must be supplied together")
        tool_names = [tool.name for tool in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("LLM tool names must be unique")


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    usage: LLMUsage
    finish_reason: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("LLM response content must be text")
        if self.reasoning_content is not None and not isinstance(
            self.reasoning_content, str
        ):
            raise TypeError("LLM reasoning_content must be text")
        call_ids = [call.call_id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("LLM tool call IDs must be unique")


@dataclass(frozen=True, slots=True)
class LLMStreamEvent:
    """One provider-independent visible delta or one completed response."""

    content_delta: str | None = None
    response: LLMResponse | None = None

    def __post_init__(self) -> None:
        if (self.content_delta is None) == (self.response is None):
            raise ValueError("stream event requires exactly one delta or response")
        if self.content_delta is not None and not self.content_delta:
            raise ValueError("stream content delta must not be empty")
