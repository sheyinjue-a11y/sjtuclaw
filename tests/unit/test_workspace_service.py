"""Workspace binding lifecycle checks."""

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.runtime import InvocationSource
from sjtuclaw.models.session import PendingRequest, Session, SessionArchiveReason
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.workspace_service import WorkspaceService, WorkspaceServiceError
from tests.fakes import FakeClock, FakeIdGenerator

NOW = datetime(2026, 7, 20, tzinfo=UTC)


def _service(sessions: InMemorySessionStore) -> WorkspaceService:
    return WorkspaceService(
        sessions=sessions,
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )


def _session(session_id: str = "session_001") -> Session:
    return Session(session_id, "Workspace test", (), "", NOW, NOW)


def test_bind_round_trip_and_replace_increments_revision(tmp_path: Path) -> None:
    sessions = InMemorySessionStore()
    sessions.create(_session())
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    service = _service(sessions)

    first = asyncio.run(service.bind("session_001", first_root))
    second = asyncio.run(service.replace("session_001", second_root))

    assert first.binding_id == "workspace_001"
    assert first.canonical_root == str(first_root.resolve())
    assert first.display_path == str(first_root)
    assert first.revision == 1
    assert second.binding_id == first.binding_id
    assert second.canonical_root == str(second_root.resolve())
    assert second.revision == 2
    assert service.binding_for("session_001") == second


def test_bind_refuses_pending_requests_without_changing_the_session(tmp_path: Path) -> None:
    sessions = InMemorySessionStore()
    sessions.create(
        Session(
            "session_001",
            "Workspace test",
            (),
            "",
            NOW,
            NOW,
            pending_requests=(
                PendingRequest(
                    "request_001",
                    1,
                    InvocationSource.CLI,
                    None,
                    "pending work",
                    NOW,
                ),
            ),
        )
    )
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(WorkspaceServiceError, match="WORKSPACE_BUSY") as error:
        asyncio.run(_service(sessions).bind("session_001", root))

    assert error.value.code == "WORKSPACE_BUSY"
    assert sessions.load("session_001").workspace_binding is None


def test_unbind_is_safe_and_leaves_session_unbound(tmp_path: Path) -> None:
    sessions = InMemorySessionStore()
    sessions.create(_session())
    root = tmp_path / "workspace"
    root.mkdir()
    service = _service(sessions)

    asyncio.run(service.unbind("session_001"))
    asyncio.run(service.bind("session_001", root))
    asyncio.run(service.unbind("session_001"))

    assert service.binding_for("session_001") is None
    assert sessions.load("session_001").workspace_binding is None


def test_bind_rejects_missing_root_and_existing_binding_becomes_unavailable(
    tmp_path: Path,
) -> None:
    sessions = InMemorySessionStore()
    sessions.create(_session())
    service = _service(sessions)

    with pytest.raises(WorkspaceServiceError, match="WORKSPACE_UNAVAILABLE"):
        asyncio.run(service.bind("session_001", tmp_path / "missing"))

    root = tmp_path / "workspace"
    root.mkdir()
    asyncio.run(service.bind("session_001", root))
    shutil.rmtree(root)

    with pytest.raises(WorkspaceServiceError, match="WORKSPACE_UNAVAILABLE"):
        service.binding_for("session_001")


def test_workspace_mutation_rejects_archived_session(tmp_path: Path) -> None:
    sessions = InMemorySessionStore()
    sessions.create(
        Session(
            "session_001",
            "Workspace test",
            (),
            "",
            NOW,
            NOW,
            archived_at=NOW,
            archived_reason=SessionArchiveReason.USER_ARCHIVED,
        )
    )
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(Exception, match="SESSION_ARCHIVED"):
        asyncio.run(_service(sessions).bind("session_001", root))

    assert sessions.load("session_001").workspace_binding is None
