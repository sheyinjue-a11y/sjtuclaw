"""Read-only tools stay inside their configured filesystem boundary."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sjtuclaw.infrastructure.tools.readonly import create_read_only_tools
from sjtuclaw.infrastructure.tools.registry import ToolRegistry
from sjtuclaw.models.tool import (
    AuthorizedToolCall,
    PermissionDecision,
    ToolCall,
    ToolSafetyLevel,
    mutable_json,
)
from sjtuclaw.runtime.permission import PermissionPolicy


def _registry(root: Path, *, max_file_bytes: int = 1024) -> ToolRegistry:
    return ToolRegistry(
        create_read_only_tools(
            root,
            now=lambda: datetime(2026, 7, 15, 12, 30, tzinfo=UTC),
            max_file_bytes=max_file_bytes,
        )
    )


def _authorize_all(
    registry: ToolRegistry, policy: PermissionPolicy, calls: tuple[ToolCall, ...]
) -> tuple[AuthorizedToolCall, ...]:
    intents = registry.validate_batch(calls)
    return tuple(policy.authorize(intent) for intent in intents)


def test_current_time_list_dir_and_read_file(tmp_path: Path) -> None:
    asyncio.run(_assert_current_time_list_dir_and_read_file(tmp_path))


async def _assert_current_time_list_dir_and_read_file(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / ".env").write_text("SECRET=never-list", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SAFE=example", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    registry = _registry(tmp_path)
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    calls = (
        ToolCall("time", "current_time", "{}"),
        ToolCall("list", "list_dir", '{"path":"."}'),
        ToolCall("read", "read_file", '{"path":"notes.txt"}'),
        ToolCall("example", "read_file", '{"path":".env.example"}'),
    )

    results = await registry.execute_batch(_authorize_all(registry, policy, calls))

    assert results[0].content == "2026-07-15T12:30:00+00:00"
    assert results[1].content.splitlines() == [".env.example", "folder/", "notes.txt"]
    assert results[2].content == "hello"
    assert results[3].content == "SAFE=example"
    assert mutable_json(results[0].structured_content) == {
        "iso8601": "2026-07-15T12:30:00+00:00"
    }
    assert mutable_json(results[1].structured_content) == {
        "path": ".",
        "entries": [
            {"name": ".env.example", "type": "file"},
            {"name": "folder", "type": "directory"},
            {"name": "notes.txt", "type": "file"},
        ],
    }
    assert mutable_json(results[2].structured_content) == {
        "path": "notes.txt",
        "size_bytes": 5,
    }
    assert mutable_json(results[3].structured_content) == {
        "path": ".env.example",
        "size_bytes": 12,
    }
    assert all(result.resources == () for result in results)
    assert all(not result.is_error for result in results)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        ".env",
        ".env.local",
        ".git/config",
        "nested/.ENV.production",
    ],
)
def test_permission_denies_escape_and_sensitive_paths(tmp_path: Path, path: str) -> None:
    registry = _registry(tmp_path)
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    intent = registry.validate_batch((ToolCall("call-1", "read_file", f'{{"path":"{path}"}}'),))[0]

    assert policy.decide(intent) is PermissionDecision.DENY
    with pytest.raises(PermissionError, match="denied"):
        policy.authorize(intent)


def test_env_example_is_explicitly_allowed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    intent = registry.validate_batch(
        (ToolCall("call-1", "read_file", '{"path":".env.example"}'),)
    )[0]

    assert policy.decide(intent) is PermissionDecision.ALLOW


def test_symlink_escape_is_denied_when_platform_supports_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    link = tmp_path / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    registry = _registry(tmp_path)
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    intent = registry.validate_batch(
        (ToolCall("call-1", "read_file", '{"path":"escape.txt"}'),)
    )[0]

    assert policy.decide(intent) is PermissionDecision.DENY


def test_symlink_alias_to_env_is_denied(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("SECRET=never", encoding="utf-8")
    link = tmp_path / "innocent.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    registry = _registry(tmp_path)
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    intent = registry.validate_batch(
        (ToolCall("call-1", "read_file", '{"path":"innocent.txt"}'),)
    )[0]

    assert policy.decide(intent) is PermissionDecision.DENY


@pytest.mark.parametrize(
    ("filename", "content", "error_code"),
    [
        ("missing.txt", None, "TOOL_EXECUTION_ERROR"),
        ("binary.bin", b"\xff\xfe", "TOOL_EXECUTION_ERROR"),
        ("large.txt", b"12345", "TOOL_EXECUTION_ERROR"),
    ],
)
def test_read_failures_are_safe_results(
    tmp_path: Path, filename: str, content: bytes | None, error_code: str
) -> None:
    asyncio.run(_assert_read_failure_is_safe(tmp_path, filename, content, error_code))


async def _assert_read_failure_is_safe(
    tmp_path: Path, filename: str, content: bytes | None, error_code: str
) -> None:
    if content is not None:
        (tmp_path / filename).write_bytes(content)
    registry = _registry(tmp_path, max_file_bytes=4)
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    calls = (ToolCall("call-1", "read_file", f'{{"path":"{filename}"}}'),)

    results = await registry.execute_batch(_authorize_all(registry, policy, calls))

    assert results[0].is_error is True
    assert results[0].error_code == error_code
    assert str(tmp_path) not in results[0].content
    decoded = "" if content is None else content.decode("utf-8", errors="ignore")
    assert not decoded or decoded not in results[0].content


def test_policy_denies_unknown_and_non_read_only_tools(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    unknown = registry.validate_batch((ToolCall("call-1", "current_time", "{}"),))[0]
    unknown = type(unknown)(
        call_id=unknown.call_id,
        name="unknown",
        arguments=unknown.arguments,
        safety_level=unknown.safety_level,
    )
    write_intent = type(unknown)(
        call_id="call-2",
        name="current_time",
        arguments={},
        safety_level=ToolSafetyLevel.WRITE,
    )

    assert policy.decide(unknown) is PermissionDecision.DENY
    assert policy.decide(write_intent) is PermissionDecision.DENY


def test_list_dir_rechecks_symlink_boundary_at_execution(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    policy = PermissionPolicy(registry.definitions(), allowed_roots=(tmp_path,))
    intent = registry.validate_batch((ToolCall("call-1", "list_dir", '{"path":"folder"}'),))[0]
    authorized = policy.authorize(intent)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside.mkdir(exist_ok=True)
    target = tmp_path / "folder"
    try:
        target.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    # The actual assertion is async, kept in a helper so the mutation occurs after authorization.
    async def execute() -> None:
        result = (await registry.execute_batch((authorized,)))[0]
        assert result.is_error is True
        assert result.error_code == "TOOL_EXECUTION_ERROR"

    asyncio.run(execute())
