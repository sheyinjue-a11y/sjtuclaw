"""Strict, atomic JSON persistence for sessions and cross-session memory."""

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from sjtuclaw.errors import StoreError
from sjtuclaw.models.memory import Memory, MemoryCategory, MemoryEntry, MemorySourceKind
from sjtuclaw.models.message import Message, MessageRole, UserInputKind
from sjtuclaw.models.run_record import RunRecord, RunRecordType
from sjtuclaw.models.runtime import InvocationSource
from sjtuclaw.models.session import (
    ApprovalDecision,
    ApprovalDecisionStatus,
    PendingApproval,
    PendingRequest,
    RunCheckpoint,
    RunState,
    RunStatus,
    Session,
    SessionArchiveReason,
    WorkspaceBinding,
)
from sjtuclaw.models.skill import SkillReference, SkillUsage, SkillUsageSource
from sjtuclaw.models.tool import (
    JsonValue,
    ToolCall,
    ToolIntent,
    ToolResourceLink,
    ToolResultStatus,
    ToolSafetyLevel,
    mutable_json,
)

SESSION_SCHEMA_VERSION = 9
MEMORY_SCHEMA_VERSION = 2
LEGACY_MEMORY_SCHEMA_VERSION = 1
LEGACY_SESSION_SCHEMA_VERSION = 1
SESSION_V2_SCHEMA_VERSION = 2
SESSION_V3_SCHEMA_VERSION = 3
SESSION_V4_SCHEMA_VERSION = 4
SESSION_V5_SCHEMA_VERSION = 5
SESSION_V6_SCHEMA_VERSION = 6
SESSION_V7_SCHEMA_VERSION = 7
SESSION_V8_SCHEMA_VERSION = 8

_SESSION_DOCUMENT_FIELDS = frozenset({"schemaVersion", "session"})
_SESSION_FIELDS_V1 = frozenset(
    {
        "sessionId",
        "title",
        "messages",
        "summary",
        "createdAt",
        "updatedAt",
        "nextSequenceNumber",
    }
)
_SESSION_FIELDS_V2 = _SESSION_FIELDS_V1 | {"runRecords"}
_SESSION_FIELDS_V3 = _SESSION_FIELDS_V2 | {"pendingRequests", "runs"}
_SESSION_FIELDS_V4 = _SESSION_FIELDS_V3 | {
    "workspaceBinding",
    "pendingApproval",
    "approvalDecisions",
}
_SESSION_FIELDS_V5 = _SESSION_FIELDS_V4 | {"explicitSkillRefs", "skillUsages"}
_SESSION_FIELDS_V6 = _SESSION_FIELDS_V5 | {
    "promptTokens",
    "promptCacheHitTokens",
    "promptCacheMissTokens",
    "completionTokens",
    "cacheBreakdownComplete",
    "tokenTrackingStartedAt",
}
_SESSION_FIELDS_V7 = _SESSION_FIELDS_V6 | {"archivedAt", "archivedReason"}
_SESSION_FIELDS_V8 = _SESSION_FIELDS_V7
_MESSAGE_FIELDS_V1 = frozenset(
    {
        "messageId",
        "role",
        "content",
        "createdAt",
        "sequenceNumber",
        "toolCallId",
    }
)
_MESSAGE_FIELDS_V2 = _MESSAGE_FIELDS_V1 | {
    "toolCalls",
    "reasoningContent",
    "toolName",
    "toolResultStatus",
    "toolErrorCode",
}
_MESSAGE_FIELDS_V3 = _MESSAGE_FIELDS_V2 | {"attachmentRefs"}
_MESSAGE_FIELDS_V3_CURRENT = _MESSAGE_FIELDS_V3 | {"inputKind"}
_MESSAGE_FIELDS_V9 = _MESSAGE_FIELDS_V3_CURRENT | {
    "tool_structured_content",
    "tool_resources",
}
_TOOL_CALL_FIELDS = frozenset({"callId", "name", "argumentsJson"})
_TOOL_RESOURCE_FIELDS = frozenset(
    {"kind", "name", "href", "mime_type", "expires_at"}
)
_RUN_RECORD_FIELDS = frozenset(
    {
        "recordId",
        "invocationId",
        "recordType",
        "code",
        "message",
        "createdAt",
        "sequenceNumber",
        "toolCallId",
        "toolName",
    }
)
_PENDING_REQUEST_FIELDS_LEGACY = frozenset(
    {
        "requestId",
        "acceptanceSequence",
        "source",
        "actorId",
        "text",
        "submittedAt",
        "attachmentRefs",
        "idempotencyKey",
    }
)
_PENDING_REQUEST_FIELDS = _PENDING_REQUEST_FIELDS_LEGACY | {
    "schedulerTaskId",
    "occurrenceId",
    "workspaceBindingRevision",
    "managedPermissionCategories",
}
_PENDING_REQUEST_FIELDS_V5 = _PENDING_REQUEST_FIELDS | {"skillRefs"}
_PENDING_REQUEST_FIELDS_V8 = _PENDING_REQUEST_FIELDS_V5 | {"automaticSkillIds"}
_RUN_FIELDS_V3_LEGACY = frozenset(
    {
        "runId",
        "requestId",
        "status",
        "startedAt",
        "completedAt",
        "durationMs",
        "assistantMessageId",
    }
)
_RUN_FIELDS = _RUN_FIELDS_V3_LEGACY | {"idempotencyKey"}
_RUN_FIELDS_V4 = _RUN_FIELDS | {"checkpoint"}
_RUN_FIELDS_V5 = _RUN_FIELDS_V4 | {"selectedSkillRefs"}
_WORKSPACE_BINDING_FIELDS = frozenset(
    {"bindingId", "canonicalRoot", "displayPath", "revision", "boundAt"}
)
_TOOL_INTENT_FIELDS = frozenset(
    {"callId", "name", "arguments", "safetyLevel", "targetResources"}
)
_PENDING_APPROVAL_FIELDS = frozenset(
    {
        "approvalId",
        "runId",
        "invocationId",
        "toolCall",
        "intent",
        "reason",
        "createdAt",
        "expiresAt",
        "workspaceBindingId",
        "workspaceRevision",
    }
)
_APPROVAL_DECISION_FIELDS = frozenset(
    {
        "decisionId",
        "approvalId",
        "status",
        "idempotencyKey",
        "actorId",
        "decidedAt",
        "reason",
    }
)
_RUN_CHECKPOINT_FIELDS_LEGACY = frozenset(
    {
        "checkpointId",
        "runId",
        "requestId",
        "pendingApprovalId",
        "nextToolCallIndex",
        "createdAt",
        "source",
        "actorId",
        "inputText",
        "attachmentRefs",
    }
)
_RUN_CHECKPOINT_FIELDS = _RUN_CHECKPOINT_FIELDS_LEGACY | {"approvedExecutionStarted"}
_RUN_CHECKPOINT_FIELDS_V5 = _RUN_CHECKPOINT_FIELDS | {"skillRefs"}
_RUN_CHECKPOINT_FIELDS_V8 = _RUN_CHECKPOINT_FIELDS_V5 | {"automaticSkillIds"}
_SKILL_REFERENCE_FIELDS = frozenset({"skillId", "snapshotId", "snapshotHash", "version"})
_SKILL_USAGE_FIELDS = frozenset(
    {
        "reference",
        "source",
        "selectionOrder",
        "reason",
        "selectedAt",
        "requestId",
        "userTask",
        "outputMessageId",
        "resourcePaths",
    }
)
_MEMORY_DOCUMENT_FIELDS = frozenset({"schemaVersion", "entries"})
_MEMORY_ENTRY_FIELDS_V1 = frozenset({"memoryId", "content", "createdAt"})
_MEMORY_ENTRY_FIELDS_V2 = frozenset(
    {
        "memoryId",
        "content",
        "category",
        "importance",
        "sourceKind",
        "sourceSessionId",
        "sourceMessageId",
        "originalTimeText",
        "eventAt",
        "eventTimezone",
        "createdAt",
        "updatedAt",
        "revision",
    }
)


class _DuplicateJsonKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return cast(object, json.load(handle, object_pairs_hook=_reject_duplicate_keys))
    except _DuplicateJsonKeyError as exc:
        raise StoreError(f"duplicate key in {path.name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StoreError(f"invalid JSON in {path.name}: {exc.msg}") from exc
    except FileNotFoundError as exc:
        raise StoreError(f"store file not found: {path.name}") from exc
    except (OSError, UnicodeError, RecursionError) as exc:
        raise StoreError(f"failed to read {path.name}: {exc}") from exc


def _atomic_write(path: Path, document: dict[str, object]) -> None:
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError) as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise StoreError(f"failed to write {path.name}: {exc}") from exc


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise StoreError(f"{context} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise StoreError(f"{context} must be an array")
    return cast(list[object], value)


def _string(value: object, context: str) -> str:
    if type(value) is not str:
        raise StoreError(f"{context} must be a string")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise StoreError(f"{context} must be an integer")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise StoreError(f"{context} must be a boolean")
    return value


def _optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _string(value, context)


def _require_fields(
    mapping: dict[str, object],
    expected: frozenset[str],
    context: str,
) -> None:
    actual = frozenset(mapping)
    if actual != expected:
        raise StoreError(
            f"{context} has invalid fields: expected {sorted(expected)}, got {sorted(actual)}"
        )


def _validate_schema(
    document: dict[str, object],
    context: str,
    supported_versions: frozenset[int],
) -> int:
    version = _integer(document["schemaVersion"], f"{context}.schemaVersion")
    if version not in supported_versions:
        raise StoreError(f"unsupported schemaVersion in {context}: {version}")
    return version


def _timestamp(value: object, context: str) -> datetime:
    raw = _string(value, context)
    try:
        timestamp = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise StoreError(f"{context} must be an ISO timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StoreError(f"{context} must be timezone-aware")
    return timestamp


def _message_to_json(message: Message) -> dict[str, object]:
    return {
        "messageId": message.message_id,
        "role": message.role.value,
        "content": message.content,
        "createdAt": message.created_at.isoformat(),
        "sequenceNumber": message.sequence_number,
        "toolCallId": message.tool_call_id,
        "toolCalls": [
            {
                "callId": call.call_id,
                "name": call.name,
                "argumentsJson": call.arguments_json,
            }
            for call in message.tool_calls
        ],
        "reasoningContent": message.reasoning_content,
        "toolName": message.tool_name,
        "toolResultStatus": (
            None if message.tool_result_status is None else message.tool_result_status.value
        ),
        "toolErrorCode": message.tool_error_code,
        "tool_structured_content": (
            None
            if message.tool_structured_content is None
            else mutable_json(message.tool_structured_content)
        ),
        "tool_resources": [
            _tool_resource_to_json(resource) for resource in message.tool_resources
        ],
        "attachmentRefs": list(message.attachment_refs),
        "inputKind": message.input_kind.value,
    }


def _message_from_json(value: object, index: int, schema_version: int) -> Message:
    context = f"session.messages[{index}]"
    data = _mapping(value, context)
    if schema_version == LEGACY_SESSION_SCHEMA_VERSION:
        _require_fields(data, _MESSAGE_FIELDS_V1, context)
    elif schema_version == SESSION_V2_SCHEMA_VERSION:
        _require_fields(data, _MESSAGE_FIELDS_V2, context)
    elif schema_version == SESSION_SCHEMA_VERSION:
        _require_fields(data, _MESSAGE_FIELDS_V9, context)
    elif frozenset(data) not in {
        _MESSAGE_FIELDS_V2,
        _MESSAGE_FIELDS_V3,
        _MESSAGE_FIELDS_V3_CURRENT,
    }:
        raise StoreError(
            f"{context} has invalid fields: expected "
            f"{sorted(_MESSAGE_FIELDS_V2)}, {sorted(_MESSAGE_FIELDS_V3)} or "
            f"{sorted(_MESSAGE_FIELDS_V3_CURRENT)}, "
            f"got {sorted(data)}"
        )
    role_value = _string(data["role"], f"{context}.role")
    try:
        role = MessageRole(role_value)
        tool_calls: tuple[ToolCall, ...] = ()
        reasoning_content: str | None = None
        tool_name: str | None = None
        tool_result_status: ToolResultStatus | None = None
        tool_error_code: str | None = None
        attachment_refs: tuple[str, ...] = ()
        input_kind = UserInputKind.USER
        tool_structured_content: Mapping[str, JsonValue] | None = None
        tool_resources: tuple[ToolResourceLink, ...] = ()
        if schema_version >= SESSION_V2_SCHEMA_VERSION:
            tool_calls = tuple(
                _tool_call_from_json(call, call_index, context)
                for call_index, call in enumerate(_list(data["toolCalls"], "toolCalls"))
            )
            reasoning_content = _optional_string(
                data["reasoningContent"], f"{context}.reasoningContent"
            )
            tool_name = _optional_string(data["toolName"], f"{context}.toolName")
            status_value = _optional_string(
                data["toolResultStatus"], f"{context}.toolResultStatus"
            )
            tool_result_status = (
                None if status_value is None else ToolResultStatus(status_value)
            )
            tool_error_code = _optional_string(
                data["toolErrorCode"], f"{context}.toolErrorCode"
            )
            if "attachmentRefs" in data:
                attachment_refs = tuple(
                    _string(
                        reference,
                        f"{context}.attachmentRefs[{reference_index}]",
                    )
                    for reference_index, reference in enumerate(
                        _list(data["attachmentRefs"], f"{context}.attachmentRefs")
                    )
                )
            if "inputKind" in data:
                input_kind = UserInputKind(
                    _string(data["inputKind"], f"{context}.inputKind")
                )
            if schema_version >= SESSION_SCHEMA_VERSION:
                raw_structured_content = data["tool_structured_content"]
                if raw_structured_content is not None:
                    tool_structured_content = cast(
                        Mapping[str, JsonValue],
                        _mapping(
                            raw_structured_content,
                            f"{context}.tool_structured_content",
                        ),
                    )
                tool_resources = tuple(
                    _tool_resource_from_json(resource, resource_index, context)
                    for resource_index, resource in enumerate(
                        _list(data["tool_resources"], f"{context}.tool_resources")
                    )
                )
        return Message(
            message_id=_string(data["messageId"], f"{context}.messageId"),
            role=role,
            content=_string(data["content"], f"{context}.content"),
            created_at=_timestamp(data["createdAt"], f"{context}.createdAt"),
            sequence_number=_integer(data["sequenceNumber"], f"{context}.sequenceNumber"),
            tool_call_id=_optional_string(data["toolCallId"], f"{context}.toolCallId"),
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            tool_name=tool_name,
            tool_result_status=tool_result_status,
            tool_error_code=tool_error_code,
            tool_structured_content=tool_structured_content,
            tool_resources=tool_resources,
            attachment_refs=attachment_refs,
            input_kind=input_kind,
        )
    except (TypeError, ValueError) as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _tool_call_from_json(value: object, index: int, message_context: str) -> ToolCall:
    context = f"{message_context}.toolCalls[{index}]"
    return _tool_call_from_json_object(value, context)


def _tool_call_from_json_object(value: object, context: str) -> ToolCall:
    data = _mapping(value, context)
    _require_fields(data, _TOOL_CALL_FIELDS, context)
    return ToolCall(
        call_id=_string(data["callId"], f"{context}.callId"),
        name=_string(data["name"], f"{context}.name"),
        arguments_json=_string(data["argumentsJson"], f"{context}.argumentsJson"),
    )


def _tool_call_to_json(call: ToolCall) -> dict[str, object]:
    return {
        "callId": call.call_id,
        "name": call.name,
        "argumentsJson": call.arguments_json,
    }


def _tool_resource_to_json(resource: ToolResourceLink) -> dict[str, object]:
    return {
        "kind": resource.kind,
        "name": resource.name,
        "href": resource.href,
        "mime_type": resource.mime_type,
        "expires_at": resource.expires_at,
    }


def _tool_resource_from_json(
    value: object,
    index: int,
    message_context: str,
) -> ToolResourceLink:
    context = f"{message_context}.tool_resources[{index}]"
    data = _mapping(value, context)
    _require_fields(data, _TOOL_RESOURCE_FIELDS, context)
    return ToolResourceLink(
        kind=_string(data["kind"], f"{context}.kind"),
        name=_string(data["name"], f"{context}.name"),
        href=_string(data["href"], f"{context}.href"),
        mime_type=_optional_string(data["mime_type"], f"{context}.mime_type"),
        expires_at=_optional_string(data["expires_at"], f"{context}.expires_at"),
    )


def _tool_intent_to_json(intent: ToolIntent) -> dict[str, object]:
    return {
        "callId": intent.call_id,
        "name": intent.name,
        "arguments": mutable_json(intent.arguments),
        "safetyLevel": intent.safety_level.value,
        "targetResources": list(intent.target_resources),
    }


def _tool_intent_from_json(value: object, context: str) -> ToolIntent:
    data = _mapping(value, context)
    _require_fields(data, _TOOL_INTENT_FIELDS, context)
    try:
        return ToolIntent(
            call_id=_string(data["callId"], f"{context}.callId"),
            name=_string(data["name"], f"{context}.name"),
            arguments=cast(
                dict[str, JsonValue],
                _mapping(data["arguments"], f"{context}.arguments"),
            ),
            safety_level=ToolSafetyLevel(
                _string(data["safetyLevel"], f"{context}.safetyLevel")
            ),
            target_resources=tuple(
                _string(resource, f"{context}.targetResources[{resource_index}]")
                for resource_index, resource in enumerate(
                    _list(data["targetResources"], f"{context}.targetResources")
                )
            ),
        )
    except (TypeError, ValueError) as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _run_record_to_json(record: RunRecord) -> dict[str, object]:
    return {
        "recordId": record.record_id,
        "invocationId": record.invocation_id,
        "recordType": record.record_type.value,
        "code": record.code,
        "message": record.message,
        "createdAt": record.created_at.isoformat(),
        "sequenceNumber": record.sequence_number,
        "toolCallId": record.tool_call_id,
        "toolName": record.tool_name,
    }


def _run_record_from_json(value: object, index: int) -> RunRecord:
    context = f"session.runRecords[{index}]"
    data = _mapping(value, context)
    _require_fields(data, _RUN_RECORD_FIELDS, context)
    try:
        return RunRecord(
            record_id=_string(data["recordId"], f"{context}.recordId"),
            invocation_id=_string(data["invocationId"], f"{context}.invocationId"),
            record_type=RunRecordType(
                _string(data["recordType"], f"{context}.recordType")
            ),
            code=_string(data["code"], f"{context}.code"),
            message=_string(data["message"], f"{context}.message"),
            created_at=_timestamp(data["createdAt"], f"{context}.createdAt"),
            sequence_number=_integer(
                data["sequenceNumber"], f"{context}.sequenceNumber"
            ),
            tool_call_id=_optional_string(data["toolCallId"], f"{context}.toolCallId"),
            tool_name=_optional_string(data["toolName"], f"{context}.toolName"),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _pending_request_to_json(request: PendingRequest) -> dict[str, object]:
    return {
        "requestId": request.request_id,
        "acceptanceSequence": request.acceptance_sequence,
        "source": request.source.value,
        "actorId": request.actor_id,
        "text": request.text,
        "submittedAt": request.submitted_at.isoformat(),
        "attachmentRefs": list(request.attachment_refs),
        "idempotencyKey": request.idempotency_key,
        "schedulerTaskId": request.scheduler_task_id,
        "occurrenceId": request.occurrence_id,
        "workspaceBindingRevision": request.workspace_binding_revision,
        "managedPermissionCategories": list(request.managed_permission_categories),
        "skillRefs": [_skill_reference_to_json(reference) for reference in request.skill_refs],
        "automaticSkillIds": list(request.automatic_skill_ids),
    }


def _pending_request_from_json(
    value: object, index: int, schema_version: int
) -> PendingRequest:
    context = f"session.pendingRequests[{index}]"
    data = _mapping(value, context)
    expected_fields = (
        {_PENDING_REQUEST_FIELDS_V8}
        if schema_version >= SESSION_V8_SCHEMA_VERSION
        else ({_PENDING_REQUEST_FIELDS_V5}
        if schema_version >= SESSION_V5_SCHEMA_VERSION
        else {_PENDING_REQUEST_FIELDS_LEGACY, _PENDING_REQUEST_FIELDS}
        )
    )
    if frozenset(data) not in expected_fields:
        raise StoreError(
            f"{context} has invalid fields: expected "
            f"{', '.join(str(sorted(fields)) for fields in expected_fields)}, got {sorted(data)}"
        )
    try:
        return PendingRequest(
            request_id=_string(data["requestId"], f"{context}.requestId"),
            acceptance_sequence=_integer(
                data["acceptanceSequence"], f"{context}.acceptanceSequence"
            ),
            source=InvocationSource(_string(data["source"], f"{context}.source")),
            actor_id=_optional_string(data["actorId"], f"{context}.actorId"),
            text=_string(data["text"], f"{context}.text"),
            submitted_at=_timestamp(data["submittedAt"], f"{context}.submittedAt"),
            attachment_refs=tuple(
                _string(reference, f"{context}.attachmentRefs[{reference_index}]")
                for reference_index, reference in enumerate(
                    _list(data["attachmentRefs"], f"{context}.attachmentRefs")
                )
            ),
            idempotency_key=_optional_string(
                data["idempotencyKey"], f"{context}.idempotencyKey"
            ),
            scheduler_task_id=(
                None
                if "schedulerTaskId" not in data
                else _optional_string(data["schedulerTaskId"], f"{context}.schedulerTaskId")
            ),
            occurrence_id=(
                None
                if "occurrenceId" not in data
                else _optional_string(data["occurrenceId"], f"{context}.occurrenceId")
            ),
            workspace_binding_revision=(
                None
                if "workspaceBindingRevision" not in data
                or data["workspaceBindingRevision"] is None
                else _integer(
                    data["workspaceBindingRevision"],
                    f"{context}.workspaceBindingRevision",
                )
            ),
            managed_permission_categories=(
                ()
                if "managedPermissionCategories" not in data
                else tuple(
                    _string(category, f"{context}.managedPermissionCategories[{category_index}]")
                    for category_index, category in enumerate(
                        _list(
                            data["managedPermissionCategories"],
                            f"{context}.managedPermissionCategories",
                        )
                    )
                )
            ),
            skill_refs=(
                ()
                if schema_version < SESSION_V5_SCHEMA_VERSION
                else tuple(
                    _skill_reference_from_json(
                        reference,
                        f"{context}.skillRefs[{reference_index}]",
                    )
                    for reference_index, reference in enumerate(
                        _list(data["skillRefs"], f"{context}.skillRefs")
                    )
                )
            ),
            automatic_skill_ids=(
                ()
                if schema_version < SESSION_V8_SCHEMA_VERSION
                else tuple(
                    _string(skill_id, f"{context}.automaticSkillIds[{skill_index}]")
                    for skill_index, skill_id in enumerate(
                        _list(data["automaticSkillIds"], f"{context}.automaticSkillIds")
                    )
                )
            ),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _workspace_binding_to_json(binding: WorkspaceBinding | None) -> object:
    if binding is None:
        return None
    return {
        "bindingId": binding.binding_id,
        "canonicalRoot": binding.canonical_root,
        "displayPath": binding.display_path,
        "revision": binding.revision,
        "boundAt": binding.bound_at.isoformat(),
    }


def _workspace_binding_from_json(value: object) -> WorkspaceBinding | None:
    if value is None:
        return None
    context = "session.workspaceBinding"
    data = _mapping(value, context)
    _require_fields(data, _WORKSPACE_BINDING_FIELDS, context)
    try:
        return WorkspaceBinding(
            binding_id=_string(data["bindingId"], f"{context}.bindingId"),
            canonical_root=_string(data["canonicalRoot"], f"{context}.canonicalRoot"),
            display_path=_string(data["displayPath"], f"{context}.displayPath"),
            revision=_integer(data["revision"], f"{context}.revision"),
            bound_at=_timestamp(data["boundAt"], f"{context}.boundAt"),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _approval_decision_to_json(decision: ApprovalDecision) -> dict[str, object]:
    return {
        "decisionId": decision.decision_id,
        "approvalId": decision.approval_id,
        "status": decision.status.value,
        "idempotencyKey": decision.idempotency_key,
        "actorId": decision.actor_id,
        "decidedAt": decision.decided_at.isoformat(),
        "reason": decision.reason,
    }


def _approval_decision_from_json(value: object, index: int) -> ApprovalDecision:
    context = f"session.approvalDecisions[{index}]"
    data = _mapping(value, context)
    _require_fields(data, _APPROVAL_DECISION_FIELDS, context)
    try:
        return ApprovalDecision(
            decision_id=_string(data["decisionId"], f"{context}.decisionId"),
            approval_id=_string(data["approvalId"], f"{context}.approvalId"),
            status=ApprovalDecisionStatus(
                _string(data["status"], f"{context}.status")
            ),
            idempotency_key=_string(
                data["idempotencyKey"], f"{context}.idempotencyKey"
            ),
            actor_id=_string(data["actorId"], f"{context}.actorId"),
            decided_at=_timestamp(data["decidedAt"], f"{context}.decidedAt"),
            reason=_optional_string(data["reason"], f"{context}.reason"),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _pending_approval_to_json(approval: PendingApproval | None) -> object:
    if approval is None:
        return None
    return {
        "approvalId": approval.approval_id,
        "runId": approval.run_id,
        "invocationId": approval.invocation_id,
        "toolCall": _tool_call_to_json(approval.tool_call),
        "intent": _tool_intent_to_json(approval.intent),
        "reason": approval.reason,
        "createdAt": approval.created_at.isoformat(),
        "expiresAt": approval.expires_at.isoformat(),
        "workspaceBindingId": approval.workspace_binding_id,
        "workspaceRevision": approval.workspace_revision,
    }


def _pending_approval_from_json(value: object) -> PendingApproval | None:
    if value is None:
        return None
    context = "session.pendingApproval"
    data = _mapping(value, context)
    _require_fields(data, _PENDING_APPROVAL_FIELDS, context)
    revision = data["workspaceRevision"]
    if revision is not None:
        revision = _integer(revision, f"{context}.workspaceRevision")
    try:
        return PendingApproval(
            approval_id=_string(data["approvalId"], f"{context}.approvalId"),
            run_id=_string(data["runId"], f"{context}.runId"),
            invocation_id=_string(data["invocationId"], f"{context}.invocationId"),
            tool_call=_tool_call_from_json_object(data["toolCall"], f"{context}.toolCall"),
            intent=_tool_intent_from_json(data["intent"], f"{context}.intent"),
            reason=_string(data["reason"], f"{context}.reason"),
            created_at=_timestamp(data["createdAt"], f"{context}.createdAt"),
            expires_at=_timestamp(data["expiresAt"], f"{context}.expiresAt"),
            workspace_binding_id=_optional_string(
                data["workspaceBindingId"], f"{context}.workspaceBindingId"
            ),
            workspace_revision=revision,
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _run_checkpoint_to_json(checkpoint: RunCheckpoint | None) -> object:
    if checkpoint is None:
        return None
    return {
        "checkpointId": checkpoint.checkpoint_id,
        "runId": checkpoint.run_id,
        "requestId": checkpoint.request_id,
        "pendingApprovalId": checkpoint.pending_approval_id,
        "nextToolCallIndex": checkpoint.next_tool_call_index,
        "createdAt": checkpoint.created_at.isoformat(),
        "source": checkpoint.source.value,
        "actorId": checkpoint.actor_id,
        "inputText": checkpoint.input_text,
        "attachmentRefs": list(checkpoint.attachment_refs),
        "approvedExecutionStarted": checkpoint.approved_execution_started,
        "skillRefs": [_skill_reference_to_json(reference) for reference in checkpoint.skill_refs],
        "automaticSkillIds": list(checkpoint.automatic_skill_ids),
    }


def _run_checkpoint_from_json(
    value: object, context: str, schema_version: int
) -> RunCheckpoint | None:
    if value is None:
        return None
    data = _mapping(value, context)
    expected_fields = (
        {_RUN_CHECKPOINT_FIELDS_V8}
        if schema_version >= SESSION_V8_SCHEMA_VERSION
        else ({_RUN_CHECKPOINT_FIELDS_V5}
        if schema_version >= SESSION_V5_SCHEMA_VERSION
        else {_RUN_CHECKPOINT_FIELDS_LEGACY, _RUN_CHECKPOINT_FIELDS}
        )
    )
    if frozenset(data) not in expected_fields:
        raise StoreError(
            f"{context} has invalid fields: expected "
            f"{', '.join(str(sorted(fields)) for fields in expected_fields)}, got {sorted(data)}"
        )
    try:
        return RunCheckpoint(
            checkpoint_id=_string(data["checkpointId"], f"{context}.checkpointId"),
            run_id=_string(data["runId"], f"{context}.runId"),
            request_id=_string(data["requestId"], f"{context}.requestId"),
            pending_approval_id=_string(
                data["pendingApprovalId"], f"{context}.pendingApprovalId"
            ),
            next_tool_call_index=_integer(
                data["nextToolCallIndex"], f"{context}.nextToolCallIndex"
            ),
            created_at=_timestamp(data["createdAt"], f"{context}.createdAt"),
            source=InvocationSource(_string(data["source"], f"{context}.source")),
            actor_id=_optional_string(data["actorId"], f"{context}.actorId"),
            input_text=_string(data["inputText"], f"{context}.inputText"),
            attachment_refs=tuple(
                _string(reference, f"{context}.attachmentRefs[{index}]")
                for index, reference in enumerate(
                    _list(data["attachmentRefs"], f"{context}.attachmentRefs")
                )
            ),
            approved_execution_started=(
                False
                if "approvedExecutionStarted" not in data
                else _boolean(
                    data["approvedExecutionStarted"],
                    f"{context}.approvedExecutionStarted",
                )
            ),
            skill_refs=(
                ()
                if schema_version < SESSION_V5_SCHEMA_VERSION
                else tuple(
                    _skill_reference_from_json(
                        reference,
                        f"{context}.skillRefs[{reference_index}]",
                    )
                    for reference_index, reference in enumerate(
                        _list(data["skillRefs"], f"{context}.skillRefs")
                    )
                )
            ),
            automatic_skill_ids=(
                ()
                if schema_version < SESSION_V8_SCHEMA_VERSION
                else tuple(
                    _string(skill_id, f"{context}.automaticSkillIds[{skill_index}]")
                    for skill_index, skill_id in enumerate(
                        _list(data["automaticSkillIds"], f"{context}.automaticSkillIds")
                    )
                )
            ),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _run_to_json(run: RunState) -> dict[str, object]:
    return {
        "runId": run.run_id,
        "requestId": run.request_id,
        "status": run.status.value,
        "startedAt": None if run.started_at is None else run.started_at.isoformat(),
        "completedAt": None
        if run.completed_at is None
        else run.completed_at.isoformat(),
        "durationMs": run.duration_ms,
        "assistantMessageId": run.assistant_message_id,
        "idempotencyKey": run.idempotency_key,
        "checkpoint": _run_checkpoint_to_json(run.checkpoint),
        "selectedSkillRefs": [
            _skill_reference_to_json(reference) for reference in run.selected_skill_refs
        ],
    }


def _run_from_json(value: object, index: int, schema_version: int) -> RunState:
    context = f"session.runs[{index}]"
    data = _mapping(value, context)
    expected_fields = (
        {_RUN_FIELDS_V5}
        if schema_version >= SESSION_V5_SCHEMA_VERSION
        else ({_RUN_FIELDS_V4} if schema_version == SESSION_V4_SCHEMA_VERSION else {_RUN_FIELDS_V3_LEGACY, _RUN_FIELDS})
    )
    if frozenset(data) not in expected_fields:
        raise StoreError(
            f"{context} has invalid fields: expected "
            f"{', '.join(str(sorted(fields)) for fields in expected_fields)}, "
            f"got {sorted(data)}"
        )
    duration = data["durationMs"]
    if duration is not None:
        duration = _integer(duration, f"{context}.durationMs")
    try:
        return RunState(
            run_id=_string(data["runId"], f"{context}.runId"),
            request_id=_string(data["requestId"], f"{context}.requestId"),
            status=RunStatus(_string(data["status"], f"{context}.status")),
            started_at=(
                None
                if data["startedAt"] is None
                else _timestamp(data["startedAt"], f"{context}.startedAt")
            ),
            completed_at=(
                None
                if data["completedAt"] is None
                else _timestamp(data["completedAt"], f"{context}.completedAt")
            ),
            duration_ms=duration,
            assistant_message_id=_optional_string(
                data["assistantMessageId"], f"{context}.assistantMessageId"
            ),
            idempotency_key=(
                None
                if "idempotencyKey" not in data
                else _optional_string(data["idempotencyKey"], f"{context}.idempotencyKey")
            ),
            checkpoint=(
                None
                if schema_version < SESSION_V4_SCHEMA_VERSION
                else _run_checkpoint_from_json(
                    data["checkpoint"], f"{context}.checkpoint", schema_version
                )
            ),
            selected_skill_refs=(
                ()
                if schema_version < SESSION_V5_SCHEMA_VERSION
                else tuple(
                    _skill_reference_from_json(
                        reference,
                        f"{context}.selectedSkillRefs[{reference_index}]",
                    )
                    for reference_index, reference in enumerate(
                        _list(data["selectedSkillRefs"], f"{context}.selectedSkillRefs")
                    )
                )
            ),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _skill_reference_to_json(reference: SkillReference) -> dict[str, object]:
    return {
        "skillId": reference.skill_id,
        "snapshotId": reference.snapshot_id,
        "snapshotHash": reference.snapshot_hash,
        "version": reference.version,
    }


def _skill_reference_from_json(value: object, context: str) -> SkillReference:
    data = _mapping(value, context)
    _require_fields(data, _SKILL_REFERENCE_FIELDS, context)
    try:
        return SkillReference(
            skill_id=_string(data["skillId"], f"{context}.skillId"),
            snapshot_id=_string(data["snapshotId"], f"{context}.snapshotId"),
            snapshot_hash=_string(data["snapshotHash"], f"{context}.snapshotHash"),
            version=_string(data["version"], f"{context}.version"),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _skill_usage_to_json(usage: SkillUsage) -> dict[str, object]:
    return {
        "reference": _skill_reference_to_json(usage.reference),
        "source": usage.source.value,
        "selectionOrder": usage.selection_order,
        "reason": usage.reason,
        "selectedAt": usage.selected_at.isoformat(),
        "requestId": usage.request_id,
        "userTask": usage.user_task,
        "outputMessageId": usage.output_message_id,
        "resourcePaths": list(usage.resource_paths),
    }


def _skill_usage_from_json(value: object, index: int) -> SkillUsage:
    context = f"session.skillUsages[{index}]"
    data = _mapping(value, context)
    _require_fields(data, _SKILL_USAGE_FIELDS, context)
    try:
        return SkillUsage(
            reference=_skill_reference_from_json(data["reference"], f"{context}.reference"),
            source=SkillUsageSource(_string(data["source"], f"{context}.source")),
            selection_order=_integer(data["selectionOrder"], f"{context}.selectionOrder"),
            reason=_string(data["reason"], f"{context}.reason"),
            selected_at=_timestamp(data["selectedAt"], f"{context}.selectedAt"),
            request_id=_string(data["requestId"], f"{context}.requestId"),
            user_task=_string(data["userTask"], f"{context}.userTask"),
            output_message_id=_optional_string(
                data["outputMessageId"], f"{context}.outputMessageId"
            ),
            resource_paths=tuple(
                _string(path, f"{context}.resourcePaths[{path_index}]")
                for path_index, path in enumerate(
                    _list(data["resourcePaths"], f"{context}.resourcePaths")
                )
            ),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _session_to_json(session: Session) -> dict[str, object]:
    return {
        "schemaVersion": SESSION_SCHEMA_VERSION,
        "session": {
            "sessionId": session.session_id,
            "title": session.title,
            "messages": [_message_to_json(message) for message in session.messages],
            "summary": session.summary,
            "createdAt": session.created_at.isoformat(),
            "updatedAt": session.updated_at.isoformat(),
            "nextSequenceNumber": session.next_sequence_number,
            "runRecords": [_run_record_to_json(record) for record in session.run_records],
            "pendingRequests": [
                _pending_request_to_json(request) for request in session.pending_requests
            ],
            "runs": [_run_to_json(run) for run in session.runs],
            "workspaceBinding": _workspace_binding_to_json(session.workspace_binding),
            "pendingApproval": _pending_approval_to_json(session.pending_approval),
            "approvalDecisions": [
                _approval_decision_to_json(decision)
                for decision in session.approval_decisions
            ],
            "explicitSkillRefs": [
                _skill_reference_to_json(reference) for reference in session.explicit_skill_refs
            ],
            "skillUsages": [_skill_usage_to_json(usage) for usage in session.skill_usages],
            "promptTokens": session.prompt_tokens,
            "promptCacheHitTokens": session.prompt_cache_hit_tokens,
            "promptCacheMissTokens": session.prompt_cache_miss_tokens,
            "completionTokens": session.completion_tokens,
            "cacheBreakdownComplete": session.cache_breakdown_complete,
            "tokenTrackingStartedAt": (
                None
                if session.token_tracking_started_at is None
                else session.token_tracking_started_at.isoformat()
            ),
            "archivedAt": (
                None if session.archived_at is None else session.archived_at.isoformat()
            ),
            "archivedReason": (
                None if session.archived_reason is None else session.archived_reason.value
            ),
        },
    }


def _session_from_json(value: object) -> Session:
    document = _mapping(value, "session document")
    _require_fields(document, _SESSION_DOCUMENT_FIELDS, "session document")
    schema_version = _validate_schema(
        document,
        "session document",
        frozenset(
            {
                LEGACY_SESSION_SCHEMA_VERSION,
                SESSION_V2_SCHEMA_VERSION,
                SESSION_V3_SCHEMA_VERSION,
                SESSION_V4_SCHEMA_VERSION,
                SESSION_V5_SCHEMA_VERSION,
                SESSION_V6_SCHEMA_VERSION,
                SESSION_V7_SCHEMA_VERSION,
                SESSION_V8_SCHEMA_VERSION,
                SESSION_SCHEMA_VERSION,
            }
        ),
    )
    data = _mapping(document["session"], "session")
    expected_fields = (
        _SESSION_FIELDS_V1
        if schema_version == LEGACY_SESSION_SCHEMA_VERSION
        else (
            _SESSION_FIELDS_V2
            if schema_version == SESSION_V2_SCHEMA_VERSION
            else (
                _SESSION_FIELDS_V3
                if schema_version == SESSION_V3_SCHEMA_VERSION
                else (
                    _SESSION_FIELDS_V4
                    if schema_version == SESSION_V4_SCHEMA_VERSION
                    else (
                        _SESSION_FIELDS_V5
                        if schema_version == SESSION_V5_SCHEMA_VERSION
                        else (
                            _SESSION_FIELDS_V6
                            if schema_version == SESSION_V6_SCHEMA_VERSION
                            else (
                                _SESSION_FIELDS_V7
                                if schema_version == SESSION_V7_SCHEMA_VERSION
                                else _SESSION_FIELDS_V8
                            )
                        )
                    )
                )
            )
        )
    )
    _require_fields(data, expected_fields, "session")
    messages = tuple(
        _message_from_json(message, index, schema_version)
        for index, message in enumerate(_list(data["messages"], "session.messages"))
    )
    run_records = (
        ()
        if schema_version == LEGACY_SESSION_SCHEMA_VERSION
        else tuple(
            _run_record_from_json(record, index)
            for index, record in enumerate(_list(data["runRecords"], "session.runRecords"))
        )
    )
    pending_requests = (
        ()
        if schema_version < SESSION_V3_SCHEMA_VERSION
        else tuple(
            _pending_request_from_json(request, index, schema_version)
            for index, request in enumerate(
                _list(data["pendingRequests"], "session.pendingRequests")
            )
        )
    )
    runs = (
        ()
        if schema_version < SESSION_V3_SCHEMA_VERSION
        else tuple(
            _run_from_json(run, index, schema_version)
            for index, run in enumerate(_list(data["runs"], "session.runs"))
        )
    )
    workspace_binding = (
        None
        if schema_version < SESSION_V4_SCHEMA_VERSION
        else _workspace_binding_from_json(data["workspaceBinding"])
    )
    pending_approval = (
        None
        if schema_version < SESSION_V4_SCHEMA_VERSION
        else _pending_approval_from_json(data["pendingApproval"])
    )
    approval_decisions = (
        ()
        if schema_version < SESSION_V4_SCHEMA_VERSION
        else tuple(
            _approval_decision_from_json(decision, index)
            for index, decision in enumerate(
                _list(data["approvalDecisions"], "session.approvalDecisions")
            )
        )
    )
    try:
        return Session(
            session_id=_string(data["sessionId"], "session.sessionId"),
            title=_string(data["title"], "session.title"),
            messages=messages,
            summary=_string(data["summary"], "session.summary"),
            created_at=_timestamp(data["createdAt"], "session.createdAt"),
            updated_at=_timestamp(data["updatedAt"], "session.updatedAt"),
            next_sequence_number=_integer(
                data["nextSequenceNumber"],
                "session.nextSequenceNumber",
            ),
            run_records=run_records,
            pending_requests=pending_requests,
            runs=runs,
            workspace_binding=workspace_binding,
            pending_approval=pending_approval,
            approval_decisions=approval_decisions,
            explicit_skill_refs=(
                ()
                if schema_version < SESSION_V5_SCHEMA_VERSION
                else tuple(
                    _skill_reference_from_json(
                        reference,
                        f"session.explicitSkillRefs[{reference_index}]",
                    )
                    for reference_index, reference in enumerate(
                        _list(data["explicitSkillRefs"], "session.explicitSkillRefs")
                    )
                )
            ),
            skill_usages=(
                ()
                if schema_version < SESSION_V5_SCHEMA_VERSION
                else tuple(
                    _skill_usage_from_json(usage, usage_index)
                    for usage_index, usage in enumerate(
                        _list(data["skillUsages"], "session.skillUsages")
                    )
                )
            ),
            prompt_tokens=(
                0
                if schema_version < SESSION_V6_SCHEMA_VERSION
                else _integer(data["promptTokens"], "session.promptTokens")
            ),
            prompt_cache_hit_tokens=(
                0
                if schema_version < SESSION_V6_SCHEMA_VERSION
                else _integer(
                    data["promptCacheHitTokens"],
                    "session.promptCacheHitTokens",
                )
            ),
            prompt_cache_miss_tokens=(
                0
                if schema_version < SESSION_V6_SCHEMA_VERSION
                else _integer(
                    data["promptCacheMissTokens"],
                    "session.promptCacheMissTokens",
                )
            ),
            completion_tokens=(
                0
                if schema_version < SESSION_V6_SCHEMA_VERSION
                else _integer(data["completionTokens"], "session.completionTokens")
            ),
            cache_breakdown_complete=(
                True
                if schema_version < SESSION_V6_SCHEMA_VERSION
                else _boolean(
                    data["cacheBreakdownComplete"],
                    "session.cacheBreakdownComplete",
                )
            ),
            token_tracking_started_at=(
                None
                if schema_version < SESSION_V6_SCHEMA_VERSION
                or data["tokenTrackingStartedAt"] is None
                else _timestamp(
                    data["tokenTrackingStartedAt"],
                    "session.tokenTrackingStartedAt",
                )
            ),
            archived_at=(
                None
                if schema_version < SESSION_V8_SCHEMA_VERSION
                or data["archivedAt"] is None
                else _timestamp(data["archivedAt"], "session.archivedAt")
            ),
            archived_reason=(
                None
                if (
                    schema_version < SESSION_V8_SCHEMA_VERSION
                    or data["archivedReason"] is None
                )
                else SessionArchiveReason(_string(data["archivedReason"], "session.archivedReason"))
            ),
        )
    except ValueError as exc:
        raise StoreError(f"invalid session: {exc}") from exc


def _memory_entry_to_json(entry: MemoryEntry) -> dict[str, object]:
    assert entry.updated_at is not None
    return {
        "memoryId": entry.memory_id,
        "content": entry.content,
        "category": entry.category.value,
        "importance": entry.importance,
        "sourceKind": entry.source_kind.value,
        "sourceSessionId": entry.source_session_id,
        "sourceMessageId": entry.source_message_id,
        "originalTimeText": entry.original_time_text,
        "eventAt": None if entry.event_at is None else entry.event_at.isoformat(),
        "eventTimezone": entry.event_timezone,
        "createdAt": entry.created_at.isoformat(),
        "updatedAt": entry.updated_at.isoformat(),
        "revision": entry.revision,
    }


def _memory_entry_from_json(value: object, index: int, schema_version: int) -> MemoryEntry:
    context = f"memory.entries[{index}]"
    data = _mapping(value, context)
    _require_fields(
        data,
        _MEMORY_ENTRY_FIELDS_V1 if schema_version == LEGACY_MEMORY_SCHEMA_VERSION else _MEMORY_ENTRY_FIELDS_V2,
        context,
    )
    try:
        created_at = _timestamp(data["createdAt"], f"{context}.createdAt")
        return MemoryEntry(
            memory_id=_string(data["memoryId"], f"{context}.memoryId"),
            content=_string(data["content"], f"{context}.content"),
            created_at=created_at,
            category=(
                MemoryCategory.OTHER
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else MemoryCategory(_string(data["category"], f"{context}.category"))
            ),
            importance=(
                3
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else _integer(data["importance"], f"{context}.importance")
            ),
            source_kind=(
                MemorySourceKind.MANUAL
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else MemorySourceKind(_string(data["sourceKind"], f"{context}.sourceKind"))
            ),
            source_session_id=(
                None
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else _optional_string(data["sourceSessionId"], f"{context}.sourceSessionId")
            ),
            source_message_id=(
                None
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else _optional_string(data["sourceMessageId"], f"{context}.sourceMessageId")
            ),
            original_time_text=(
                None
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else _optional_string(data["originalTimeText"], f"{context}.originalTimeText")
            ),
            event_at=(
                None
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION or data["eventAt"] is None
                else _timestamp(data["eventAt"], f"{context}.eventAt")
            ),
            event_timezone=(
                None
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else _optional_string(data["eventTimezone"], f"{context}.eventTimezone")
            ),
            updated_at=(
                created_at
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else _timestamp(data["updatedAt"], f"{context}.updatedAt")
            ),
            revision=(
                1
                if schema_version == LEGACY_MEMORY_SCHEMA_VERSION
                else _integer(data["revision"], f"{context}.revision")
            ),
        )
    except ValueError as exc:
        raise StoreError(f"invalid {context}: {exc}") from exc


def _memory_to_json(memory: Memory) -> dict[str, object]:
    return {
        "schemaVersion": MEMORY_SCHEMA_VERSION,
        "entries": [_memory_entry_to_json(entry) for entry in memory.entries],
    }


def _require_unique_memory_ids(memory: Memory) -> None:
    memory_ids = [entry.memory_id for entry in memory.entries]
    if len(memory_ids) != len(set(memory_ids)):
        raise StoreError("duplicate memory_id in memory document")


def _memory_from_json(value: object) -> Memory:
    document = _mapping(value, "memory document")
    _require_fields(document, _MEMORY_DOCUMENT_FIELDS, "memory document")
    schema_version = _validate_schema(
        document,
        "memory document",
        frozenset({LEGACY_MEMORY_SCHEMA_VERSION, MEMORY_SCHEMA_VERSION}),
    )
    entries = tuple(
        _memory_entry_from_json(entry, index, schema_version)
        for index, entry in enumerate(_list(document["entries"], "memory.entries"))
    )
    memory_ids = [entry.memory_id for entry in entries]
    if len(memory_ids) != len(set(memory_ids)):
        raise StoreError("duplicate memory_id in memory document")
    return Memory(entries)


class JsonSessionStore:
    """One strict JSON document per Session."""

    def __init__(self, root: Path) -> None:
        self._sessions_dir = root / "sessions"

    def _path(self, session_id: str) -> Path:
        try:
            digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        except UnicodeError as exc:
            raise StoreError("session_id cannot be encoded as UTF-8") from exc
        return self._sessions_dir / f"{digest}.json"

    def _load_path(self, path: Path, expected_session_id: str | None = None) -> Session:
        session = _session_from_json(_read_json(path))
        if expected_session_id is not None and session.session_id != expected_session_id:
            raise StoreError(f"session_id does not match file {path.name}")
        return session

    def create(self, session: Session) -> None:
        path = self._path(session.session_id)
        if path.exists():
            raise StoreError(f"session already exists: {session.session_id}")
        _atomic_write(path, _session_to_json(session))

    def list(self, *, archived: bool = False) -> tuple[Session, ...]:
        if not self._sessions_dir.exists():
            return ()
        if not self._sessions_dir.is_dir():
            raise StoreError("sessions path is not a directory")
        try:
            paths: Iterable[Path] = sorted(self._sessions_dir.glob("*.json"))
        except OSError as exc:
            raise StoreError(f"failed to list sessions: {exc}") from exc

        sessions: list[Session] = []
        loaded_paths: list[tuple[Path, Session]] = []
        session_ids: set[str] = set()
        for path in paths:
            session = self._load_path(path)
            if session.session_id in session_ids:
                raise StoreError(f"duplicate session_id in session files: {session.session_id}")
            session_ids.add(session.session_id)
            if (session.archived_at is not None) == archived:
                sessions.append(session)
            loaded_paths.append((path, session))

        for path, session in loaded_paths:
            if path.name != self._path(session.session_id).name:
                raise StoreError(f"session file name does not match session_id: {path.name}")

        by_id = sorted(sessions, key=lambda session: session.session_id)
        return tuple(sorted(by_id, key=lambda session: session.updated_at, reverse=True))

    def load(self, session_id: str) -> Session:
        return self._load_path(self._path(session_id), session_id)

    def save(self, session: Session) -> None:
        path = self._path(session.session_id)
        self._load_path(path, session.session_id)
        _atomic_write(path, _session_to_json(session))

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        self._load_path(path, session_id)
        try:
            path.unlink()
        except OSError as exc:
            raise StoreError(f"failed to delete session {session_id}: {exc}") from exc


class JsonMemoryStore:
    """Strict cross-session Memory persisted in root/memory.json."""

    def __init__(self, root: Path) -> None:
        self._path = root / "memory.json"

    def load(self) -> Memory:
        if not self._path.exists():
            return Memory()
        return _memory_from_json(_read_json(self._path))

    def replace(self, memory: Memory) -> Memory:
        _require_unique_memory_ids(memory)
        if self._path.exists():
            self.load()
        _atomic_write(self._path, _memory_to_json(memory))
        return memory

    def add(self, entry: MemoryEntry) -> Memory:
        memory = self.load()
        if any(existing.memory_id == entry.memory_id for existing in memory.entries):
            raise StoreError(f"memory already exists: {entry.memory_id}")
        updated = Memory((*memory.entries, entry))
        return self.replace(updated)

    def delete(self, memory_id: str) -> Memory:
        memory = self.load()
        if not any(entry.memory_id == memory_id for entry in memory.entries):
            raise StoreError(f"memory not found: {memory_id}")
        updated = Memory(tuple(entry for entry in memory.entries if entry.memory_id != memory_id))
        return self.replace(updated)
