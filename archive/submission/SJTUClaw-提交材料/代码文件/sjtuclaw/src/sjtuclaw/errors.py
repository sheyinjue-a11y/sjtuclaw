"""Stable error taxonomy shared by runtime and entrypoints."""


class SJTUClawError(Exception):
    """Base class for expected SJTUClaw failures."""


class ConfigurationError(SJTUClawError):
    """Configuration is missing or invalid."""


class LLMError(SJTUClawError):
    """The LLM provider request failed."""


class StoreError(SJTUClawError):
    """Persistent state could not be loaded or saved safely."""


class AttachmentError(SJTUClawError):
    """An attachment operation failed without exposing storage internals."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ContextBudgetError(SJTUClawError):
    """The immutable context already exceeds its allowed budget."""


class ToolProtocolError(SJTUClawError):
    """The model returned an invalid tool protocol payload."""


class ToolValidationError(SJTUClawError):
    """A normalized tool request failed validation."""


class ToolExecutionError(SJTUClawError):
    """A validated tool failed during execution."""


class AgentLimitError(SJTUClawError):
    """An agent-loop safety limit was reached."""


class InvocationCancelled(SJTUClawError):
    """The current invocation was cancelled."""


class ApprovalRequired(SJTUClawError):
    """A durable one-call approval checkpoint paused the current Run."""

    def __init__(self, approval_id: str) -> None:
        super().__init__("APPROVAL_REQUIRED")
        self.approval_id = approval_id


class SkillError(SJTUClawError):
    """A local Skill is unavailable, invalid, or outside its bounded contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
