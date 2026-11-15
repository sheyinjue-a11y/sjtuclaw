"""Step 5 permission policy for registered read-only tools."""

from collections.abc import Sequence
from pathlib import Path

from sjtuclaw.models.tool import (
    AuthorizedToolCall,
    PermissionDecision,
    ToolDefinition,
    ToolExecutionContext,
    ToolIntent,
    ToolSafetyLevel,
)


class PermissionPolicy:
    """Allow safe registered reads; deny everything else in Step 5."""

    def __init__(
        self,
        definitions: Sequence[ToolDefinition],
        *,
        allowed_roots: Sequence[Path],
    ) -> None:
        by_name: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if definition.name in by_name:
                raise ValueError(f"duplicate tool definition: {definition.name}")
            by_name[definition.name] = definition
        roots = tuple(root.resolve(strict=False) for root in allowed_roots)
        if not roots:
            raise ValueError("permission policy requires at least one allowed root")
        self._definitions = by_name
        self._allowed_roots = roots

    def decide(
        self,
        intent: ToolIntent,
        context: ToolExecutionContext | None = None,
    ) -> PermissionDecision:
        definition = self._definitions.get(intent.name)
        if definition is None:
            return PermissionDecision.DENY
        if definition.safety_level is not intent.safety_level:
            return PermissionDecision.DENY
        if intent.safety_level is ToolSafetyLevel.READ_ONLY:
            if intent.name == "read_skill_resource":
                return (
                    PermissionDecision.ALLOW
                    if context is not None and context.selected_skill_refs
                    else PermissionDecision.DENY
                )
            if context is not None and context.workspace_binding_id is not None:
                # The handler reconstructs a PathGuard from this trusted binding
                # for every call.  Its stale TOOL_ROOT target must not override
                # the stronger Workspace check.
                return PermissionDecision.ALLOW
            if any(not self._resource_allowed(target) for target in intent.target_resources):
                return PermissionDecision.DENY
            return PermissionDecision.ALLOW
        if context is not None and self._has_managed_scheduler_grant(intent, context):
            return PermissionDecision.ALLOW
        # Write/execute effects are revalidated with the trusted Workspace
        # context only after a durable one-call approval is recorded.
        return PermissionDecision.ASK

    def authorize(
        self,
        intent: ToolIntent,
        context: ToolExecutionContext | None = None,
    ) -> AuthorizedToolCall:
        decision = self.decide(intent, context)
        if decision is not PermissionDecision.ALLOW:
            raise PermissionError("tool call denied by permission policy")
        return AuthorizedToolCall.from_permission(intent, decision)

    def authorize_approved(self, intent: ToolIntent) -> AuthorizedToolCall:
        """Create a capability after the matching persisted approval decision."""

        definition = self._definitions.get(intent.name)
        if definition is None or definition.safety_level is not intent.safety_level:
            raise PermissionError("approved tool call is no longer registered")
        if intent.safety_level is ToolSafetyLevel.READ_ONLY:
            if any(not self._resource_allowed(target) for target in intent.target_resources):
                raise PermissionError("approved read-only tool call is no longer permitted")
        return AuthorizedToolCall.from_permission(intent, PermissionDecision.ALLOW)

    def _resource_allowed(self, target: str) -> bool:
        path = Path(target)
        if _has_sensitive_component(path):
            return False
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            return False
        if _has_sensitive_component(resolved):
            return False
        return any(resolved.is_relative_to(root) for root in self._allowed_roots)

    @staticmethod
    def _has_managed_scheduler_grant(
        intent: ToolIntent,
        context: ToolExecutionContext,
    ) -> bool:
        if (
            context.source.value != "scheduler"
            or not context.scheduler_task_id
            or not context.occurrence_id
        ):
            return False
        if (
            context.workspace_revision is None
            or context.workspace_root is None
            or context.scheduler_workspace_binding_revision != context.workspace_revision
        ):
            return False
        category = {
            "copy_attachment_to_workspace": "workspace_write",
            "write_file": "workspace_write",
            "new_shell": "shell",
            "run_command": "shell",
            "create_download": "download",
        }.get(intent.name)
        return category is not None and category in context.managed_permission_categories


def _has_sensitive_component(path: Path) -> bool:
    for part in path.parts:
        name = part.casefold()
        if name == ".git":
            return True
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return True
    return False
