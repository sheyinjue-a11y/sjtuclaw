from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from sjtuclaw.entrypoints.gateway import GatewayOptions, create_gateway
from sjtuclaw.infrastructure.persistence import (
    JsonAttachmentStore,
    JsonSessionStore,
    JsonTaskStore,
)
from sjtuclaw.infrastructure.tools.downloads import DownloadSnapshotService
from sjtuclaw.models.llm import LLMResponse, LLMUsage
from sjtuclaw.models.scheduler import Occurrence, OccurrenceStatus
from sjtuclaw.runtime import (
    ArchiveService,
    AttachmentService,
    ContextBuilder,
    RuntimeHost,
    SchedulerService,
    SessionCoordinator,
    SessionService,
    WorkspaceService,
)
from tests.fakes import FakeClock, FakeIdGenerator, FakeLLMClient

NOW = datetime(2026, 7, 20, tzinfo=UTC)


def test_gateway_archive_is_read_only_restore_keeps_tasks_archived_and_purge_cascades(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    ids = FakeIdGenerator()
    coordinator = SessionCoordinator()
    session_store = JsonSessionStore(tmp_path)
    sessions = SessionService(
        sessions=session_store,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    session = sessions.create("archive")
    attachment_store = JsonAttachmentStore(tmp_path)
    attachments = AttachmentService(
        sessions=session_store,
        attachments=attachment_store,
        coordinator=coordinator,
    )
    runtime = RuntimeHost(
        llm=FakeLLMClient([LLMResponse("unused", LLMUsage(1, 1, 2), "stop")]),
        sessions=session_store,
        context_builder=ContextBuilder("system"),
        coordinator=coordinator,
        clock=clock,
        ids=ids,
        attachments=attachment_store,
    )
    task_store = JsonTaskStore(tmp_path)
    scheduler = SchedulerService(
        tasks=task_store,
        runtime=runtime,
        sessions=session_store,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    workspace = WorkspaceService(
        sessions=session_store,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    downloads = DownloadSnapshotService(root=tmp_path, clock=clock, ids=ids)
    archive = ArchiveService(
        sessions=session_store,
        coordinator=coordinator,
        runtime=runtime,
        scheduler=scheduler,
        attachments=attachment_store,
        downloads=downloads,
        clock=clock,
    )
    options = GatewayOptions(port=8765, bootstrap_token="z" * 43)

    with TestClient(
        create_gateway(
            sessions=sessions,
            runtime=runtime,
            attachments=attachments,
            scheduler=scheduler,
            archive=archive,
            workspace=workspace,
            downloads=downloads,
            options=options,
        ),
        base_url=options.origin,
    ) as client:
        _bootstrap(client, options)
        uploaded = client.post(
            f"/api/v1/sessions/{session.session_id}/attachments",
            headers={
                "Origin": options.origin,
                "Content-Type": "text/plain",
                "X-Attachment-Name": "notes.txt",
            },
            content=b"private attachment",
        )
        assert uploaded.status_code == 201
        created = client.post(
            "/api/v1/tasks",
            headers={"Origin": options.origin},
            json={
                "sessionId": session.session_id,
                "instruction": "future task",
                "schedule": {
                    "kind": "once",
                    "anchorAt": (NOW + timedelta(hours=1)).isoformat(),
                },
            },
        )
        assert created.status_code == 201
        task_id = created.json()["task"]["taskId"]
        failed = Occurrence(
            occurrence_id="occurrence_failed",
            task_id=task_id,
            sequence_number=1,
            status=OccurrenceStatus.FAILED,
            scheduled_for=NOW,
            created_at=NOW,
            updated_at=NOW,
            queued_at=NOW,
            run_id="run_failed",
            request_id="request_failed",
            error_code="LLM_ERROR",
        )
        task_store.create_occurrence(failed)

        archived = client.post(
            f"/api/v1/sessions/{session.session_id}/archive",
            headers={"Origin": options.origin},
        )
        assert archived.status_code == 200
        assert archived.json()["session"]["archivedAt"] is not None
        assert task_store.load_task(task_id).archived_at is not None

        blocked = [
            client.patch(
                f"/api/v1/sessions/{session.session_id}",
                headers={"Origin": options.origin},
                json={"title": "changed"},
            ),
            client.post(
                f"/api/v1/sessions/{session.session_id}/requests",
                headers={"Origin": options.origin},
                json={"text": "must not run"},
            ),
            client.post(
                f"/api/v1/sessions/{session.session_id}/attachments",
                headers={
                    "Origin": options.origin,
                    "Content-Type": "text/plain",
                    "X-Attachment-Name": "new.txt",
                },
                content=b"must not save",
            ),
            client.put(
                f"/api/v1/sessions/{session.session_id}/workspace",
                headers={"Origin": options.origin},
                json={"root": str(tmp_path)},
            ),
            client.post(
                "/api/v1/tasks",
                headers={"Origin": options.origin},
                json={
                    "sessionId": session.session_id,
                    "instruction": "must not schedule",
                    "schedule": {
                        "kind": "once",
                        "anchorAt": (NOW + timedelta(hours=2)).isoformat(),
                    },
                },
            ),
        ]
        assert [response.status_code for response in blocked] == [409] * len(blocked)
        assert {response.json()["code"] for response in blocked} == {"SESSION_ARCHIVED"}

        retried = client.post(
            f"/api/v1/tasks/{task_id}/occurrences/{failed.occurrence_id}/retry",
            headers={"Origin": options.origin},
        )
        assert retried.status_code == 409
        assert retried.json()["code"] == "OCCURRENCE_NOT_RETRYABLE"

        restored = client.post(
            f"/api/v1/sessions/{session.session_id}/restore",
            headers={"Origin": options.origin},
        )
        assert restored.status_code == 200
        assert restored.json()["session"]["archivedAt"] is None
        assert task_store.load_task(task_id).archived_at is not None

        assert client.post(
            f"/api/v1/sessions/{session.session_id}/archive",
            headers={"Origin": options.origin},
        ).status_code == 200
        purged = client.delete(
            f"/api/v1/sessions/{session.session_id}",
            headers={"Origin": options.origin},
        )
        assert purged.status_code == 204

    assert not list((tmp_path / "sessions").glob("*.json"))
    assert task_store.list_tasks(archived=True) == ()
    assert not list((tmp_path / "attachments").rglob("metadata.json"))
    asyncio.run(runtime.aclose())


def _bootstrap(client: TestClient, options: GatewayOptions) -> None:
    response = client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": options.origin},
        json={"token": options.bootstrap_token},
    )
    assert response.status_code == 204
