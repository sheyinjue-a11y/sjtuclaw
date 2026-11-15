from datetime import UTC, datetime, timedelta

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.models.session import Session


def test_in_memory_store_has_deterministic_listing_and_crud() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    store = InMemorySessionStore()
    older = Session("session_001", "older", (), "", now, now)
    newer = Session("session_002", "newer", (), "", now, now + timedelta(seconds=1))

    store.create(older)
    store.create(newer)

    assert [session.session_id for session in store.list()] == ["session_002", "session_001"]
    assert store.load("session_001") == older
    store.delete("session_001")
    with pytest.raises(StoreError, match="session not found"):
        store.load("session_001")
