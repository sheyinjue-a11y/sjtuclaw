"""Task-local trusted context for tool execution."""

from contextvars import ContextVar, Token

from sjtuclaw.models.tool import ToolExecutionContext

_CURRENT_CONTEXT: ContextVar[ToolExecutionContext | None] = ContextVar(
    "sjtuclaw_tool_execution_context",
    default=None,
)


def set_execution_context(context: ToolExecutionContext | None) -> Token[ToolExecutionContext | None]:
    return _CURRENT_CONTEXT.set(context)


def reset_execution_context(token: Token[ToolExecutionContext | None]) -> None:
    _CURRENT_CONTEXT.reset(token)


def require_execution_context() -> ToolExecutionContext:
    context = _CURRENT_CONTEXT.get()
    if context is None:
        raise RuntimeError("tool execution context is not available")
    return context
