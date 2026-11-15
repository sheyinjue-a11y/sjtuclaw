from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.infrastructure.persistence import (
    JsonAttachmentStore,
    JsonSessionStore,
    JsonTaskStore,
)
from sjtuclaw.infrastructure.tools.downloads import (
    DownloadServiceError,
    DownloadSnapshotService,
)
from sjtuclaw.models.llm import LLMRequest, LLMResponse
from sjtuclaw.models.runtime import InvocationSource, RuntimeRequest, UserInput
from sjtuclaw.models.scheduler import Occurrence, OccurrenceStatus, Schedule, ScheduleKind
from sjtuclaw.models.session import RunState, RunStatus, Session, SessionArchiveReason
from sjtuclaw.runtime import ContextBuilder, RuntimeHost
from sjtuclaw.runtime.archive_service import ArchiveService
from sjtuclaw.runtime.scheduler import SchedulerService
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_service import SessionService
from tests.fakes import FakeClock, FakeIdGenerator

NOW = datetime(2026, 7, 20, tzinfo=UTC)


async def _chunks() -> AsyncIterator[bytes]:
    yield b"attachment bytes"


class _Runtime:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def stop_session(self, session_id: str) -> None:
        self.stopped.append(session_id)

    async def submit(self, _request: object) -> object:
        raise AssertionError("archive test must not submit")

    def find_run(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def cancel_queued(self, _session_id: str, _request_id: str) -> bool:
        return False

    async def stop_run(self, _session_id: str, _run_id: str) -> bool:
        return False


class _UnusedLLM:
    async def complete(self, _request: LLMRequest) -> LLMResponse:
        raise AssertionError("orphaned queued run must not start")


class _FlakyScheduler:
    def __init__(self, sessions: JsonSessionStore) -> None:
        self.sessions = sessions
        self.calls = 0

    async def archive_for_session(self, session_id: str) -> tuple[()]:
        assert self.sessions.load(session_id).archived_at is not None
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("scheduler unavailable")
        return ()

    async def purge_for_session(self, _session_id: str) -> None:
        return None


def test_archive_marks_session_first_and_retry_recovers_mid_operation_failure(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sessions = JsonSessionStore(tmp_path)
        sessions.create(Session("session_001", "archive", (), "", NOW, NOW))
        runtime = _Runtime()
        scheduler = _FlakyScheduler(sessions)
        service = ArchiveService(
            sessions=sessions,
            coordinator=SessionCoordinator(),
            runtime=runtime,
            scheduler=scheduler,
            attachments=JsonAttachmentStore(tmp_path),
            downloads=DownloadSnapshotService(
                root=tmp_path,
                clock=FakeClock(NOW),
                ids=FakeIdGenerator(),
            ),
            clock=FakeClock(NOW),
        )

        with pytest.raises(RuntimeError, match="scheduler unavailable"):
            await service.archive_session("session_001")

        assert sessions.load("session_001").archived_at == NOW
        archived = await service.archive_session("session_001")
        assert archived.archived_at == NOW
        assert runtime.stopped == ["session_001", "session_001"]
        assert scheduler.calls == 2

    asyncio.run(scenario())


def test_purge_cascades_durable_tasks_occurrences_attachments_and_downloads(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sessions = JsonSessionStore(tmp_path)
        coordinator = SessionCoordinator()
        clock = FakeClock(NOW)
        session_service = SessionService(
            sessions=sessions,
            coordinator=coordinator,
            clock=clock,
            ids=FakeIdGenerator(),
        )
        session = session_service.create("cascade")
        runtime = _Runtime()
        task_store = JsonTaskStore(tmp_path)
        scheduler = SchedulerService(
            tasks=task_store,
            runtime=runtime,
            sessions=sessions,
            coordinator=coordinator,
            clock=clock,
            ids=FakeIdGenerator(),
        )
        task = await scheduler.create(
            session_id=session.session_id,
            instruction="future work",
            schedule=Schedule(ScheduleKind.ONCE, NOW + timedelta(hours=1)),
        )
        task_store.create_occurrence(
            Occurrence(
                occurrence_id="occurrence_001",
                task_id=task.task_id,
                sequence_number=1,
                status=OccurrenceStatus.PLANNED,
                scheduled_for=NOW + timedelta(hours=1),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        attachments = JsonAttachmentStore(tmp_path)
        await attachments.ingest_text(
            session_id=session.session_id,
            attachment_id="attachment_001",
            display_name="notes.txt",
            chunks=_chunks(),
            created_at=NOW,
        )
        source = tmp_path / "source.txt"
        source.write_text("download", encoding="utf-8")
        downloads = DownloadSnapshotService(
            root=tmp_path,
            clock=clock,
            ids=FakeIdGenerator(),
        )
        download = downloads.publish(
            session_id=session.session_id,
            source=source,
            filename="result.txt",
        )
        service = ArchiveService(
            sessions=sessions,
            coordinator=coordinator,
            runtime=runtime,
            scheduler=scheduler,
            attachments=attachments,
            downloads=downloads,
            clock=clock,
        )

        await service.archive_session(session.session_id)
        await service.purge_session(session.session_id)
        await service.purge_session(session.session_id)

        with pytest.raises(StoreError):
            sessions.load(session.session_id)
        assert task_store.list_tasks() == ()
        assert task_store.list_tasks(archived=True) == ()
        assert not (tmp_path / "scheduler" / "occurrences" / task.task_id).exists()
        assert attachments.list(session.session_id) == ()
        assert not list((tmp_path / "attachments").rglob("metadata.json"))
        with pytest.raises(DownloadServiceError, match="DOWNLOAD_NOT_FOUND"):
            downloads.lease(download_id=download.download_id)

    asyncio.run(scenario())


def test_archive_stops_active_runtime_before_completing_transition(tmp_path: Path) -> None:
    async def scenario() -> None:
        class BlockingLLM:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.never = asyncio.Event()

            async def complete(self, _request: LLMRequest) -> LLMResponse:
                self.started.set()
                await self.never.wait()
                raise AssertionError("blocked run must be cancelled")

        sessions = JsonSessionStore(tmp_path)
        sessions.create(Session("session_001", "active", (), "", NOW, NOW))
        coordinator = SessionCoordinator()
        clock = FakeClock(NOW)
        llm = BlockingLLM()
        runtime = RuntimeHost(
            llm=llm,
            sessions=sessions,
            context_builder=ContextBuilder("system"),
            coordinator=coordinator,
            clock=clock,
            ids=FakeIdGenerator(),
        )
        scheduler = SchedulerService(
            tasks=JsonTaskStore(tmp_path),
            runtime=runtime,
            sessions=sessions,
            coordinator=coordinator,
            clock=clock,
            ids=FakeIdGenerator(),
        )
        service = ArchiveService(
            sessions=sessions,
            coordinator=coordinator,
            runtime=runtime,
            scheduler=scheduler,
            attachments=JsonAttachmentStore(tmp_path),
            downloads=DownloadSnapshotService(
                root=tmp_path,
                clock=clock,
                ids=FakeIdGenerator(),
            ),
            clock=clock,
        )
        handle = await runtime.submit(
            RuntimeRequest(
                "request_001",
                "session_001",
                InvocationSource.GATEWAY,
                UserInput("block"),
            )
        )
        await llm.started.wait()

        archived = await service.archive_session("session_001")
        events = [event async for event in handle.events()]

        assert archived.archived_at == NOW
        assert archived.pending_requests == ()
        assert archived.runs[0].status is RunStatus.CANCELLED
        assert events[-1].event_type.value == "run_cancelled"
        await runtime.aclose()

    asyncio.run(scenario())


def test_archive_recovers_an_archived_session_with_orphaned_queued_runs(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sessions = JsonSessionStore(tmp_path)
        sessions.create(
            Session(
                "session_001",
                "orphaned queue",
                (),
                "",
                NOW,
                NOW,
                runs=(RunState("run_001", "request_001", RunStatus.QUEUED),),
                archived_at=NOW,
                archived_reason=SessionArchiveReason.USER_ARCHIVED,
            )
        )
        coordinator = SessionCoordinator()
        clock = FakeClock(NOW)
        runtime = RuntimeHost(
            llm=_UnusedLLM(),
            sessions=sessions,
            context_builder=ContextBuilder("system"),
            coordinator=coordinator,
            clock=clock,
            ids=FakeIdGenerator(),
        )
        service = ArchiveService(
            sessions=sessions,
            coordinator=coordinator,
            runtime=runtime,
            scheduler=SchedulerService(
                tasks=JsonTaskStore(tmp_path),
                runtime=runtime,
                sessions=sessions,
                coordinator=coordinator,
                clock=clock,
                ids=FakeIdGenerator(),
            ),
            attachments=JsonAttachmentStore(tmp_path),
            downloads=DownloadSnapshotService(
                root=tmp_path,
                clock=clock,
                ids=FakeIdGenerator(),
            ),
            clock=clock,
        )

        archived = await service.archive_session("session_001")

        assert archived.archived_at == NOW
        assert archived.runs[0].status is RunStatus.CANCELLED_BEFORE_START
        await runtime.aclose()

    asyncio.run(scenario())
