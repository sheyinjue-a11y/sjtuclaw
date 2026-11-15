"""Whole-batch validation and deterministic tool execution."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from sjtuclaw.errors import ToolExecutionError
from sjtuclaw.infrastructure.tools.registry import ToolRegistry
from sjtuclaw.models.tool import (
    AuthorizedToolCall,
    JsonValue,
    PermissionDecision,
    ToolCall,
    ToolDefinition,
    ToolExecutionOutput,
    ToolIntent,
    ToolResourceLink,
    ToolResult,
    ToolResultStatus,
    ToolSafetyLevel,
)
from sjtuclaw.ports.tools import ToolBatchValidationError


@dataclass
class RecordingTool:
    name: str
    executions: list[str]
    fail: bool = False

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="record execution order",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ("value",),
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ("value",),
                "additionalProperties": False,
            },
            result_usage="Use value as the recorded test output.",
            safety_level=ToolSafetyLevel.READ_ONLY,
        )

    def target_resources(self, arguments: Mapping[str, object]) -> tuple[str, ...]:
        return ()

    async def execute(self, arguments: Mapping[str, object]) -> ToolExecutionOutput:
        value = arguments["value"]
        assert isinstance(value, str)
        self.executions.append(value)
        if self.fail:
            raise ToolExecutionError("deliberate safe failure")
        rendered = value.upper()
        return ToolExecutionOutput(
            content=rendered,
            structured_content={"value": rendered},
        )


@dataclass
class OutputTool:
    output: ToolExecutionOutput
    output_schema: Mapping[str, JsonValue]
    allowed_resource_kinds: tuple[str, ...] = ()

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="output_tool",
            description="return a controlled typed output",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema=self.output_schema,
            result_usage="Use this controlled output in tests.",
            safety_level=ToolSafetyLevel.READ_ONLY,
            allowed_resource_kinds=self.allowed_resource_kinds,
        )

    def target_resources(self, arguments: Mapping[str, object]) -> tuple[str, ...]:
        return ()

    async def execute(self, arguments: Mapping[str, object]) -> ToolExecutionOutput:
        return self.output


def _authorize(intent: ToolIntent) -> AuthorizedToolCall:
    return AuthorizedToolCall.from_permission(intent, PermissionDecision.ALLOW)


def test_registry_lists_definitions_in_registration_order() -> None:
    registry = ToolRegistry((RecordingTool("first", []), RecordingTool("second", [])))

    assert [definition.name for definition in registry.definitions()] == ["first", "second"]


@pytest.mark.parametrize("count", [0, 6])
def test_batch_size_must_be_between_one_and_five(count: int) -> None:
    registry = ToolRegistry((RecordingTool("record", []),))
    calls = tuple(
        ToolCall(call_id=f"call-{index}", name="record", arguments_json='{"value":"ok"}')
        for index in range(count)
    )

    with pytest.raises(ToolBatchValidationError) as caught:
        registry.validate_batch(calls)

    if calls:
        assert len(caught.value.results) == len(calls)
        assert {result.error_code for result in caught.value.results} == {"BATCH_SIZE_INVALID"}


def test_malformed_json_rejects_entire_batch_with_zero_execution() -> None:
    executions: list[str] = []
    registry = ToolRegistry((RecordingTool("record", executions),))
    calls = (
        ToolCall("call-1", "record", '{"value":"would-run"}'),
        ToolCall("call-2", "record", "{malformed"),
    )

    with pytest.raises(ToolBatchValidationError) as caught:
        registry.validate_batch(calls)

    assert executions == []
    assert caught.value.results[0].error_code == "BATCH_REJECTED"
    assert caught.value.results[1].error_code == "INVALID_ARGUMENTS_JSON"


def test_duplicate_call_ids_are_rejected_before_json_and_schema_validation() -> None:
    executions: list[str] = []
    registry = ToolRegistry((RecordingTool("record", executions),))
    calls = (
        ToolCall("duplicate", "record", '{"value":"first"}'),
        ToolCall("unique", "record", "{malformed"),
        ToolCall("duplicate", "record", '{"value":"third"}'),
    )

    with pytest.raises(ToolBatchValidationError) as caught:
        registry.validate_batch(calls)

    assert executions == []
    assert [result.error_code for result in caught.value.results] == [
        "DUPLICATE_CALL_ID",
        "BATCH_REJECTED",
        "DUPLICATE_CALL_ID",
    ]
    assert all(
        call.arguments_json not in result.content
        for call, result in zip(calls, caught.value.results, strict=True)
    )


@pytest.mark.parametrize(
    ("call", "error_code"),
    [
        (ToolCall("call-1", "missing", "{}"), "UNKNOWN_TOOL"),
        (ToolCall("call-1", "record", "[]"), "ARGUMENTS_NOT_OBJECT"),
        (ToolCall("call-1", "record", '{"unexpected":true}'), "SCHEMA_VALIDATION_FAILED"),
        (ToolCall("call-1", "record", '{"value":"a","value":"b"}'), "INVALID_ARGUMENTS_JSON"),
        (ToolCall("call-1", "record", '{"value":NaN}'), "INVALID_ARGUMENTS_JSON"),
    ],
)
def test_registry_reports_safe_structured_validation_errors(
    call: ToolCall, error_code: str
) -> None:
    registry = ToolRegistry((RecordingTool("record", []),))

    with pytest.raises(ToolBatchValidationError) as caught:
        registry.validate_batch((call,))

    result = caught.value.results[0]
    assert result.call_id == call.call_id
    assert result.tool_name == call.name
    assert result.is_error is True
    assert result.error_code == error_code
    assert call.arguments_json not in result.content


def test_authorized_calls_execute_in_order_and_one_failure_does_not_stop_batch() -> None:
    asyncio.run(_assert_authorized_calls_execute_in_order_and_continue())


async def _assert_authorized_calls_execute_in_order_and_continue() -> None:
    executions: list[str] = []
    registry = ToolRegistry(
        (
            RecordingTool("ok", executions),
            RecordingTool("fail", executions, fail=True),
        )
    )
    intents = registry.validate_batch(
        (
            ToolCall("call-1", "ok", '{"value":"first"}'),
            ToolCall("call-2", "fail", '{"value":"second"}'),
            ToolCall("call-3", "ok", '{"value":"third"}'),
        )
    )

    results = await registry.execute_batch(tuple(_authorize(intent) for intent in intents))

    assert executions == ["first", "second", "third"]
    assert [(result.call_id, result.is_error) for result in results] == [
        ("call-1", False),
        ("call-2", True),
        ("call-3", False),
    ]
    assert results[1].error_code == "TOOL_EXECUTION_ERROR"
    assert results[1].content == "deliberate safe failure"


def test_executor_rejects_a_call_not_registered_in_this_registry() -> None:
    asyncio.run(_assert_executor_rejects_unregistered_call())


async def _assert_executor_rejects_unregistered_call() -> None:
    registry = ToolRegistry((RecordingTool("known", []),))
    foreign = ToolIntent(
        call_id="call-x",
        name="unknown",
        arguments={},
        safety_level=ToolSafetyLevel.READ_ONLY,
    )

    results = await registry.execute_batch((_authorize(foreign),))

    assert results == (
        ToolResult(
            call_id="call-x",
            tool_name="unknown",
            content="tool is not registered",
            status=ToolResultStatus.ERROR,
            error_code="UNKNOWN_TOOL",
        ),
    )


@pytest.mark.parametrize(
    "tool",
    [
        OutputTool(
            output=ToolExecutionOutput(
                content="wrong output",
                structured_content={"count": "not-an-integer"},
            ),
            output_schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ("count",),
                "additionalProperties": False,
            },
        ),
        OutputTool(
            output=ToolExecutionOutput(
                content="undeclared resource",
                structured_content={},
                resources=(ToolResourceLink("image", "preview", "/preview/one"),),
            ),
            output_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        OutputTool(
            output=ToolExecutionOutput(
                content="invalid download href",
                structured_content={},
                resources=(ToolResourceLink("download", "report.txt", "/files/report.txt"),),
            ),
            output_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            allowed_resource_kinds=("download",),
        ),
        OutputTool(
            output=ToolExecutionOutput(
                content="too many resources",
                structured_content={},
                resources=tuple(
                    ToolResourceLink("download", f"report-{index}", f"/downloads/id_{index}")
                    for index in range(17)
                ),
            ),
            output_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            allowed_resource_kinds=("download",),
        ),
        OutputTool(
            output=ToolExecutionOutput(
                content="resource metadata too long",
                structured_content={},
                resources=(
                    ToolResourceLink(
                        "download",
                        "n" * 256,
                        "/downloads/report",
                    ),
                ),
            ),
            output_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            allowed_resource_kinds=("download",),
        ),
    ],
    ids=(
        "invalid-structured-content",
        "undeclared-resource",
        "invalid-download-href",
        "too-many-resources",
        "resource-metadata-too-long",
    ),
)
def test_registry_rejects_invalid_typed_tool_outputs_without_leaking_details(
    tool: OutputTool,
) -> None:
    async def execute() -> ToolResult:
        registry = ToolRegistry((tool,))
        intent = registry.validate_batch((ToolCall("call-1", "output_tool", "{}"),))[0]
        return (await registry.execute_batch((_authorize(intent),)))[0]

    result = asyncio.run(execute())

    assert result == ToolResult(
        call_id="call-1",
        tool_name="output_tool",
        content="tool returned an invalid result",
        status=ToolResultStatus.ERROR,
        error_code="TOOL_OUTPUT_SCHEMA_INVALID",
    )
    assert result.structured_content is None
    assert result.resources == ()
