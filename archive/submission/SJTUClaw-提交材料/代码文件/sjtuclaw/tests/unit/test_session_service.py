import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.session import SessionArchiveReason
from sjtuclaw.runtime import SessionCoordinator, SessionService
from tests.fakes import FakeClock, FakeIdGenerator


def test_session_service_manages_sessions() -> None:
    asyncio.run(_assert_session_service_manages_sessions())


async def _assert_session_service_manages_sessions() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    sessions = InMemorySessionStore()
    service = SessionService(
        sessions=sessions,
        coordinator=SessionCoordinator(),
        clock=FakeClock(now),
        ids=FakeIdGenerator(),
    )

    created = service.create()
    renamed = await service.rename(created.session_id, "课程问答")

    assert created.session_id == "session_001"
    assert renamed.title == "课程问答"
    assert service.load(created.session_id) == renamed
    assert service.list() == (renamed,)

    await service.delete(created.session_id)
    assert service.list() == ()


def test_session_service_rejects_rename_for_archived_session() -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 15, tzinfo=UTC)
        store = InMemorySessionStore()
        service = SessionService(
            sessions=store,
            coordinator=SessionCoordinator(),
            clock=FakeClock(now),
            ids=FakeIdGenerator(),
        )
        created = service.create()
        store.save(
            replace(
                created,
                archived_at=now,
                archived_reason=SessionArchiveReason.USER_ARCHIVED,
            )
        )

        with pytest.raises(Exception, match="SESSION_ARCHIVED"):
            await service.rename(created.session_id, "must not change")

        assert store.load(created.session_id).title == created.title

    asyncio.run(scenario())
