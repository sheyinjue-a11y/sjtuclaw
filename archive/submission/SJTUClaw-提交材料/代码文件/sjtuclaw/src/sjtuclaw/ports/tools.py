"""Provider-independent tool registry and execution contracts."""

from collections.abc import Sequence
from typing import Protocol

from sjtuclaw.errors import ToolValidationError
from sjtuclaw.models.tool import (
    AuthorizedToolCall,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolIntent,
    ToolResult,
)


class ToolBatchValidationError(ToolValidationError):
    """A whole tool batch was rejected before any execution."""

    def __init__(self, message: str, results: tuple[ToolResult, ...]) -> None:
        super().__init__(message)
        self.results = results


class ToolExecutor(Protocol):
    def definitions(self) -> tuple[ToolDefinition, ...]: ...

    def validate_batch(self, calls: Sequence[ToolCall]) -> tuple[ToolIntent, ...]: ...

    async def execute_batch(
        self,
        calls: Sequence[AuthorizedToolCall],
        *,
        context: ToolExecutionContext | None = None,
    ) -> tuple[ToolResult, ...]: ...

    async def close_context(self, context: ToolExecutionContext) -> None: ...
