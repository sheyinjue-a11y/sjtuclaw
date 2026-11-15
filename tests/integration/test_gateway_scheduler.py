"""Gateway Task API contracts over the durable SchedulerService."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from sjtuclaw.entrypoints.gateway import GatewayOptions, create_gateway
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.infrastructure.persistence.scheduler import JsonTaskStore
from sjtuclaw.models.runtime import RuntimeRequest
from sjtuclaw.models.scheduler import OccurrenceStatus
from sjtuclaw.models.session import RunState, RunStatus
from sjtuclaw.runtime import SchedulerService, SessionCoordinator, SessionService
from tests.fakes import FakeClock, FakeIdGenerator


class _Runtime:
    def __init__(self) -> None:
        self.runs: dict[str, RunState] = {}

    async def submit(self, request: RuntimeRequest) -> object:
        self.runs[request.idempotency_key or request.request_id] = RunState(
            run_id="run_001",
            request_id=request.request_id,
            status=RunStatus.QUEUED,
            idempotency_key=request.idempotency_key,
        )
        return object()

    def find_run(
        self,
        _session_id: str,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunState | None:
        if idempotency_key is not None:
            return self.runs.get(idempotency_key)
        return next((run for run in self.runs.values() if run.request_id == request_id), None)

    async def cancel_queued(self, _session_id: str, request_id: str) -> bool:
        for key, run in self.runs.items():
            if run.request_id == request_id:
                self.runs[key] = replace(run, status=RunStatus.CANCELLED_BEFORE_START)
                return True
        return False

    async def stop_run(self, _session_id: str, run_id: str) -> bool:
        for key, run in self.runs.items():
            if run.run_id == run_id and run.status is RunStatus.STARTED:
                self.runs[key] = replace(run, status=RunStatus.CANCELLED)
                return True
        return False


def test_gateway_manages_scheduler_tasks_and_occurrence_history(tmp_path: Path) -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    clock = FakeClock(now)
    session_store = InMemorySessionStore()
    coordinator = SessionCoordinator()
    sessions = SessionService(
        sessions=session_store,
        coordinator=coordinator,
        clock=clock,
        ids=FakeIdGenerator(),
    )
    session = sessions.create("scheduler")
    store = JsonTaskStore(tmp_path)
    scheduler = SchedulerService(
        tasks=store,
        runtime=_Runtime(),
        sessions=session_store,
        coordinator=coordinator,
        clock=clock,
        ids=FakeIdGenerator(),
    )
    options = GatewayOptions(port=8765, bootstrap_token="t" * 43)

    with TestClient(
        create_gateway(sessions=sessions, scheduler=scheduler, options=options),
        base_url=options.origin,
    ) as client:
        _bootstrap(client, options)
        created = client.post(
            "/api/v1/tasks",
            headers={"Origin": options.origin},
            json={
                "sessionId": session.session_id,
                "instruction": "run scheduled summary",
                "schedule": {
                    "kind": "once",
                    "anchorAt": (now + timedelta(minutes=1)).isoformat(),
                    "intervalSeconds": None,
                    "timezone": None,
                    "hour": None,
                    "minute": None,
                    "weekdays": [],
                },
                "managedPermissions": [],
                "skillRefs": [],
            },
        )
        assert created.status_code == 201
        task = created.json()["task"]
        assert task["sessionId"] == session.session_id
        assert task["nextRunAt"] == (now + timedelta(minutes=1)).isoformat()

        listed = client.get(f"/api/v1/tasks?session_id={session.session_id}")
        assert listed.status_code == 200
        assert listed.json()["tasks"] == [task]
        assert client.get(f"/api/v1/tasks/{task['taskId']}").json()["task"] == task

        clock.current += timedelta(minutes=1)
        asyncio.run(scheduler.tick())
        occurrence, _ = store.list_occurrences(task["taskId"])
        failed = replace(
            occurrence[0], status=OccurrenceStatus.FAILED, error_code="LLM_ERROR"
        )
        store.save_occurrence(failed)

        history = client.get(f"/api/v1/tasks/{task['taskId']}/occurrences")
        assert history.status_code == 200
        assert history.json()["occurrences"][0]["status"] == "failed"

        retried = client.post(
            f"/api/v1/tasks/{task['taskId']}/occurrences/{failed.occurrence_id}/retry",
            headers={"Origin": options.origin},
        )
        assert retried.status_code == 200
        assert retried.json()["occurrence"]["retryOfOccurrenceId"] == failed.occurrence_id

        cancelled = client.post(
            f"/api/v1/tasks/{task['taskId']}/cancel",
            headers={"Origin": options.origin},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["task"]["status"] == "cancelled"

        invalid = client.post(
            "/api/v1/tasks",
            headers={"Origin": options.origin},
            json={
                "sessionId": session.session_id,
                "instruction": "past once",
                "schedule": {"kind": "once", "anchorAt": now.isoformat()},
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "INVALID_SCHEDULE"


def _bootstrap(client: TestClient, options: GatewayOptions) -> None:
    response = client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": options.origin},
        json={"token": options.bootstrap_token},
    )
    assert response.status_code == 204
