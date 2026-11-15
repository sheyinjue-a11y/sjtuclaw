"""Run reusable Store contracts against JSON persistence."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sjtuclaw.infrastructure.persistence import JsonMemoryStore, JsonSessionStore
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.session import Session
from sjtuclaw.models.tool import ToolResourceLink, ToolResultStatus
from sjtuclaw.ports.stores import MemoryStore, SessionStore
from tests.contract.memory_store_contract import MemoryStoreContract
from tests.contract.session_store_contract import SessionStoreContract


class TestJsonSessionStoreContract(SessionStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> SessionStore:
        return JsonSessionStore(tmp_path)

    def test_typed_tool_result_round_trip(self, store: SessionStore) -> None:
        now = datetime(2026, 7, 30, tzinfo=UTC)
        resource = ToolResourceLink(
            "download",
            "report.md",
            "/downloads/download_123",
            "text/markdown",
        )
        message = Message(
            "message_001",
            MessageRole.TOOL,
            "report ready",
            now,
            1,
            tool_call_id="call_001",
            tool_name="create_download",
            tool_result_status=ToolResultStatus.SUCCESS,
            tool_structured_content={"filename": "report.md"},
            tool_resources=(resource,),
        )
        session = Session("session_001", "typed", (message,), "", now, now, 2)

        store.create(session)

        assert store.load(session.session_id) == session


class TestJsonMemoryStoreContract(MemoryStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> MemoryStore:
        return JsonMemoryStore(tmp_path)
