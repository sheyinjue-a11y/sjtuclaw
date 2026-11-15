"""Strict whole-batch validation and deterministic tool execution."""

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import NoReturn, Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

from sjtuclaw.errors import ToolExecutionError
from sjtuclaw.infrastructure.tools.context import reset_execution_context, set_execution_context
from sjtuclaw.models.tool import (
    AuthorizedToolCall,
    JsonValue,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionOutput,
    ToolIntent,
    ToolResourceLink,
    ToolResult,
    ToolResultStatus,
    freeze_json,
    mutable_json,
)
from sjtuclaw.ports.tools import ToolBatchValidationError


class ToolHandler(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def target_resources(self, arguments: Mapping[str, JsonValue]) -> tuple[str, ...]: ...

    async def execute(self, arguments: Mapping[str, JsonValue]) -> ToolExecutionOutput: ...


@runtime_checkable
class ContextClosingTool(Protocol):
    async def close_context(self, context: ToolExecutionContext) -> None: ...


class ToolRegistry:
    """Registry validates every call before allowing any item to execute."""

    def __init__(self, tools: Sequence[ToolHandler]) -> None:
        ordered = tuple(tools)
        by_name: dict[str, ToolHandler] = {}
        input_validators: dict[str, Draft202012Validator] = {}
        output_validators: dict[str, Draft202012Validator] = {}
        for tool in ordered:
            definition = tool.definition
            if definition.name in by_name:
                raise ValueError(f"duplicate tool name: {definition.name}")
            input_schema = mutable_json(definition.input_schema)
            output_schema = mutable_json(definition.output_schema)
            try:
                Draft202012Validator.check_schema(input_schema)
                Draft202012Validator.check_schema(output_schema)
            except SchemaError as error:
                raise ValueError(f"invalid schema for tool {definition.name}") from error
            if any(kind != "download" for kind in definition.allowed_resource_kinds):
                raise ValueError(f"invalid resource kind for tool {definition.name}")
            by_name[definition.name] = tool
            input_validators[definition.name] = Draft202012Validator(input_schema)
            output_validators[definition.name] = Draft202012Validator(
                output_schema,
                format_checker=FormatChecker(),
            )
        self._ordered = ordered
        self._by_name = by_name
        self._input_validators = input_validators
        self._output_validators = output_validators

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._ordered)

    def validate_batch(self, calls: Sequence[ToolCall]) -> tuple[ToolIntent, ...]:
        batch = tuple(calls)
        if not 1 <= len(batch) <= 5:
            results = tuple(
                _error_result(call, "BATCH_SIZE_INVALID", "tool batch must contain 1 to 5 calls")
                for call in batch
            )
            raise ToolBatchValidationError("tool batch must contain 1 to 5 calls", results)

        call_id_counts = Counter(call.call_id for call in batch)
        duplicate_call_ids = {
            call_id for call_id, count in call_id_counts.items() if count > 1
        }
        if duplicate_call_ids:
            results = tuple(
                _error_result(
                    call,
                    "DUPLICATE_CALL_ID" if call.call_id in duplicate_call_ids else "BATCH_REJECTED",
                    (
                        "tool call_id must be unique within a batch"
                        if call.call_id in duplicate_call_ids
                        else "tool batch rejected because another call_id is duplicated"
                    ),
                )
                for call in batch
            )
            raise ToolBatchValidationError("tool batch contains duplicate call_id values", results)

        intents: list[ToolIntent] = []
        issues: dict[int, tuple[str, str]] = {}
        for index, call in enumerate(batch):
            tool = self._by_name.get(call.name)
            if tool is None:
                issues[index] = ("UNKNOWN_TOOL", "tool is not registered")
                continue
            try:
                arguments = _parse_arguments(call.arguments_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                issues[index] = ("INVALID_ARGUMENTS_JSON", "tool arguments are not valid JSON")
                continue
            if not isinstance(arguments, Mapping):
                issues[index] = ("ARGUMENTS_NOT_OBJECT", "tool arguments must be a JSON object")
                continue
            mutable_arguments = mutable_json(arguments)
            if (
                next(
                    self._input_validators[call.name].iter_errors(mutable_arguments),
                    None,
                )
                is not None
            ):
                issues[index] = (
                    "SCHEMA_VALIDATION_FAILED",
                    "tool arguments do not match the registered schema",
                )
                continue
            intents.append(
                ToolIntent(
                    call_id=call.call_id,
                    name=call.name,
                    arguments=arguments,
                    safety_level=tool.definition.safety_level,
                    target_resources=tool.target_resources(arguments),
                )
            )

        if issues:
            results = tuple(
                _error_result(
                    call,
                    *issues.get(
                        index,
                        ("BATCH_REJECTED", "tool batch rejected because another call is invalid"),
                    ),
                )
                for index, call in enumerate(batch)
            )
            raise ToolBatchValidationError("tool batch validation failed", results)
        return tuple(intents)

    async def execute_batch(
        self,
        calls: Sequence[AuthorizedToolCall],
        *,
        context: ToolExecutionContext | None = None,
    ) -> tuple[ToolResult, ...]:
        token = set_execution_context(context)
        try:
            results: list[ToolResult] = []
            for authorized in calls:
                intent = authorized.intent
                tool = self._by_name.get(intent.name)
                if tool is None:
                    results.append(
                        ToolResult(
                            call_id=intent.call_id,
                            tool_name=intent.name,
                            content="tool is not registered",
                            status=ToolResultStatus.ERROR,
                            error_code="UNKNOWN_TOOL",
                        )
                    )
                    continue
                try:
                    output = await tool.execute(intent.arguments)
                except ToolExecutionError as error:
                    results.append(
                        ToolResult(
                            call_id=intent.call_id,
                            tool_name=intent.name,
                            content=str(error),
                            status=ToolResultStatus.ERROR,
                            error_code="TOOL_EXECUTION_ERROR",
                        )
                    )
                    continue
                if not self._valid_output(tool.definition, output):
                    results.append(
                        ToolResult(
                            call_id=intent.call_id,
                            tool_name=intent.name,
                            content="tool returned an invalid result",
                            status=ToolResultStatus.ERROR,
                            error_code="TOOL_OUTPUT_SCHEMA_INVALID",
                        )
                    )
                    continue
                results.append(
                    ToolResult(
                        call_id=intent.call_id,
                        tool_name=intent.name,
                        content=output.content,
                        structured_content=output.structured_content,
                        resources=output.resources,
                    )
                )
            return tuple(results)
        finally:
            reset_execution_context(token)

    def _valid_output(
        self,
        definition: ToolDefinition,
        output: object,
    ) -> bool:
        if not isinstance(output, ToolExecutionOutput):
            return False
        structured_content = mutable_json(output.structured_content)
        if (
            next(
                self._output_validators[definition.name].iter_errors(structured_content),
                None,
            )
            is not None
        ):
            return False
        if len(output.resources) > 16:
            return False
        return all(
            _valid_resource(resource, definition.allowed_resource_kinds)
            for resource in output.resources
        )

    async def close_context(self, context: ToolExecutionContext) -> None:
        """Release volatile tool resources when the owning Run becomes terminal."""

        for tool in self._ordered:
            if isinstance(tool, ContextClosingTool):
                await tool.close_context(context)


def _parse_arguments(arguments_json: str) -> Mapping[str, JsonValue] | JsonValue:
    parsed: object = json.loads(
        arguments_json,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    return freeze_json(parsed)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


def _error_result(call: ToolCall, code: str, message: str) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.name or "invalid_tool",
        content=message,
        status=ToolResultStatus.ERROR,
        error_code=code,
    )


_DOWNLOAD_HREF = re.compile(r"^/downloads/[A-Za-z0-9_-]{1,256}$")


def _valid_resource(
    resource: object,
    allowed_resource_kinds: tuple[str, ...],
) -> bool:
    if not isinstance(resource, ToolResourceLink):
        return False
    if (
        not isinstance(resource.kind, str)
        or not isinstance(resource.name, str)
        or not isinstance(resource.href, str)
        or (
            resource.mime_type is not None
            and not isinstance(resource.mime_type, str)
        )
        or (
            resource.expires_at is not None
            and not isinstance(resource.expires_at, str)
        )
    ):
        return False
    if resource.kind not in allowed_resource_kinds:
        return False
    if (
        len(resource.kind) > 64
        or len(resource.name) > 255
        or (resource.mime_type is not None and len(resource.mime_type) > 255)
        or (resource.expires_at is not None and len(resource.expires_at) > 64)
    ):
        return False
    if not _safe_host_relative_href(resource.href):
        return False
    return resource.kind != "download" or _DOWNLOAD_HREF.fullmatch(resource.href) is not None


def _safe_host_relative_href(href: str) -> bool:
    if any(ord(character) < 32 or ord(character) == 127 for character in href):
        return False
    if "\\" in href:
        return False
    parsed = urlsplit(href)
    if (
        not href.startswith("/")
        or href.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
    ):
        return False
    decoded_path = unquote(parsed.path)
    return ".." not in decoded_path.split("/")
