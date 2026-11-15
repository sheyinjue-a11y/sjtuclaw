"""Provider-independent tool, permission, and approval contracts."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias
from urllib.parse import urlsplit

from sjtuclaw.models.runtime import InvocationSource
from sjtuclaw.models.skill import SkillReference

JsonScalar: TypeAlias = str | int | float | bool | None  # noqa: UP040
JsonValue: TypeAlias = (  # noqa: UP040
    JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)

_AUTHORIZATION_SENTINEL = object()


def freeze_json(value: object) -> JsonValue:
    """Return an immutable JSON-compatible value without retaining caller state."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def mutable_json(value: JsonValue) -> object:
    """Convert a frozen JSON value to ordinary JSON containers for integrations."""

    if isinstance(value, Mapping):
        return {key: mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [mutable_json(item) for item in value]
    return value


class ToolSafetyLevel(StrEnum):
    READ_ONLY = "read_only"
    WRITE = "write"
    EXECUTE = "execute"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Trusted invocation facts supplied by RuntimeHost, never by tool arguments."""

    session_id: str
    request_id: str
    source: InvocationSource
    actor_id: str | None = None
    workspace_binding_id: str | None = None
    workspace_revision: int | None = None
    workspace_root: Path | None = None
    selected_skill_snapshot_ids: tuple[str, ...] = ()
    selected_skill_refs: tuple[SkillReference, ...] = ()
    scheduler_task_id: str | None = None
    occurrence_id: str | None = None
    scheduler_workspace_binding_revision: int | None = None
    managed_permission_categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.request_id.strip():
            raise ValueError("tool execution context identifiers must not be empty")
        if self.workspace_revision is not None and self.workspace_revision < 1:
            raise ValueError("workspace_revision must be positive")
        if (
            self.scheduler_workspace_binding_revision is not None
            and self.scheduler_workspace_binding_revision < 1
        ):
            raise ValueError("scheduler workspace revision must be positive")
        if (self.workspace_binding_id is None) != (self.workspace_root is None):
            raise ValueError("workspace binding id and root must be supplied together")
        reference_ids = tuple(reference.snapshot_id for reference in self.selected_skill_refs)
        if self.selected_skill_snapshot_ids != reference_ids:
            raise ValueError("selected skill snapshot IDs must match trusted references")


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolResourceLink:
    kind: str
    name: str
    href: str
    mime_type: str | None = None
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not isinstance(self.name, str):
            raise TypeError("tool resource kind and name must be text")
        if not self.kind.strip() or not self.name.strip():
            raise ValueError("tool resource kind and name must not be empty")
        if not isinstance(self.href, str):
            raise TypeError("tool resource href must be text")
        parsed_href = urlsplit(self.href)
        if (
            not self.href.startswith("/")
            or self.href.startswith("//")
            or "\\" in self.href
            or parsed_href.scheme
            or parsed_href.netloc
        ):
            raise ValueError("tool resource href must be a host-relative path")


@dataclass(frozen=True, slots=True)
class ToolExecutionOutput:
    content: str
    structured_content: Mapping[str, JsonValue]
    resources: tuple[ToolResourceLink, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("tool execution output content must be text")
        frozen_content = freeze_json(self.structured_content)
        if not isinstance(frozen_content, Mapping):
            raise TypeError("tool execution output structured_content must be an object")
        object.__setattr__(self, "structured_content", frozen_content)
        object.__setattr__(self, "resources", tuple(self.resources))


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, JsonValue]
    output_schema: Mapping[str, JsonValue]
    result_usage: str
    safety_level: ToolSafetyLevel
    allowed_resource_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must not be empty")
        if not self.description.strip():
            raise ValueError("tool description must not be empty")
        if not self.result_usage.strip():
            raise ValueError("tool result_usage must not be empty")
        frozen_input_schema = freeze_json(self.input_schema)
        if not isinstance(frozen_input_schema, Mapping):
            raise TypeError("tool input_schema must be an object")
        frozen_output_schema = freeze_json(self.output_schema)
        if not isinstance(frozen_output_schema, Mapping):
            raise TypeError("tool output_schema must be an object")
        if frozen_output_schema.get("type") != "object":
            raise ValueError("tool output_schema must describe an object")
        allowed_resource_kinds = tuple(self.allowed_resource_kinds)
        if any(
            not isinstance(kind, str) or not kind.strip()
            for kind in allowed_resource_kinds
        ):
            raise ValueError("tool allowed resource kinds must not be empty")
        if len(set(allowed_resource_kinds)) != len(allowed_resource_kinds):
            raise ValueError("tool allowed resource kinds must not contain duplicates")
        object.__setattr__(self, "input_schema", frozen_input_schema)
        object.__setattr__(self, "output_schema", frozen_output_schema)
        object.__setattr__(self, "allowed_resource_kinds", allowed_resource_kinds)

    @property
    def effect(self) -> ToolSafetyLevel:
        """Compatibility name used by future permission and workspace designs."""

        return self.safety_level


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Normalized provider call retaining raw arguments for strict validation."""

    call_id: str
    name: str
    arguments_json: str

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("tool call_id must not be empty")
        if not isinstance(self.arguments_json, str):
            raise TypeError("tool arguments_json must be text")


@dataclass(frozen=True, slots=True)
class ToolIntent:
    """A schema-valid call enriched with execution and resource metadata."""

    call_id: str
    name: str
    arguments: Mapping[str, JsonValue]
    safety_level: ToolSafetyLevel
    target_resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.call_id.strip() or not self.name.strip():
            raise ValueError("tool intent identifiers must not be empty")
        frozen_arguments = freeze_json(self.arguments)
        if not isinstance(frozen_arguments, Mapping):
            raise TypeError("tool intent arguments must be an object")
        if any(not target.strip() for target in self.target_resources):
            raise ValueError("tool target resources must not be empty")
        object.__setattr__(self, "arguments", frozen_arguments)

    @property
    def effect(self) -> ToolSafetyLevel:
        return self.safety_level


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedToolCall:
    """Execution capability created only from an explicit allow decision."""

    intent: ToolIntent

    def __init__(self, intent: ToolIntent, *, _token: object | None = None) -> None:
        if _token is not _AUTHORIZATION_SENTINEL:
            raise TypeError("AuthorizedToolCall must be created by PermissionPolicy")
        object.__setattr__(self, "intent", intent)

    @classmethod
    def from_permission(
        cls,
        intent: ToolIntent,
        decision: PermissionDecision,
    ) -> "AuthorizedToolCall":
        if decision is not PermissionDecision.ALLOW:
            raise ValueError("only an allow decision can authorize a tool call")
        return cls(intent=intent, _token=_AUTHORIZATION_SENTINEL)


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_name: str
    content: str
    status: ToolResultStatus = ToolResultStatus.SUCCESS
    error_code: str | None = None
    structured_content: Mapping[str, JsonValue] | None = None
    resources: tuple[ToolResourceLink, ...] = ()

    def __post_init__(self) -> None:
        if not self.call_id.strip() or not self.tool_name.strip():
            raise ValueError("tool result identifiers must not be empty")
        if not isinstance(self.content, str):
            raise TypeError("tool result content must be text")
        if self.status is ToolResultStatus.ERROR and not self.error_code:
            raise ValueError("error tool results require an error_code")
        if self.status is ToolResultStatus.SUCCESS and self.error_code is not None:
            raise ValueError("successful tool results cannot have an error_code")
        resources = tuple(self.resources)
        if self.status is ToolResultStatus.ERROR and resources:
            raise ValueError("error tool results cannot have resources")
        if self.structured_content is not None:
            frozen_content = freeze_json(self.structured_content)
            if not isinstance(frozen_content, Mapping):
                raise TypeError("tool result structured_content must be an object")
            object.__setattr__(self, "structured_content", frozen_content)
        object.__setattr__(self, "resources", resources)

    @property
    def is_error(self) -> bool:
        return self.status is ToolResultStatus.ERROR


@dataclass(frozen=True, slots=True)
class PendingApproval:
    approval_id: str
    intent: ToolIntent
    reason: str

    def __post_init__(self) -> None:
        if not self.approval_id.strip():
            raise ValueError("approval_id must not be empty")
        if not self.reason.strip():
            raise ValueError("approval reason must not be empty")
