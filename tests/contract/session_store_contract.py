"""Backend-neutral contract tests for SessionStore implementations."""

from datetime import UTC, datetime, timedelta

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.models.message import Message, MessageRole
from sjtuclaw.models.run_record import RunRecord, RunRecordType
from sjtuclaw.models.session import Session
from sjtuclaw.models.tool import ToolCall, ToolResultStatus
from sjtuclaw.ports.stores import SessionStore

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def make_session(
    session_id: str,
    *,
    title: str = "测试会话",
    updated_at: datetime = NOW,
) -> Session:
    messages = (
        Message("msg_000", MessageRole.SYSTEM, "系统", NOW, 0),
        Message("msg_001", MessageRole.USER, "你好", NOW, 1),
        Message(
            "msg_002",
            MessageRole.ASSISTANT,
            "",
            NOW + timedelta(seconds=1),
            2,
            tool_calls=(ToolCall("call_001", "read_file", '{"path":"README.md"}'),),
            reasoning_content="需要读取文件",
        ),
        Message(
            "msg_003",
            MessageRole.TOOL,
            "结果",
            NOW + timedelta(seconds=2),
            3,
            tool_call_id="call_001",
            tool_name="read_file",
            tool_result_status=ToolResultStatus.SUCCESS,
        ),
    )
    record = RunRecord(
        "record_001",
        "request_001",
        RunRecordType.STATUS,
        "TOOL_COMPLETED",
        "tool completed",
        NOW + timedelta(seconds=3),
        1,
        "call_001",
        "read_file",
    )
    return Session(
        session_id,
        title,
        messages,
        "摘要",
        NOW,
        updated_at,
        4,
        (record,),
    )


class SessionStoreContract:
    """Mixin inherited by every concrete SessionStore contract suite."""

    def test_create_load_save_delete_round_trip(self, store: SessionStore) -> None:
        original = make_session("session_001")
        store.create(original)

        assert store.load(original.session_id) == original

        changed = Session(
            original.session_id,
            "已重命名",
            original.messages,
            original.summary,
            original.created_at,
            original.updated_at + timedelta(minutes=1),
            original.next_sequence_number,
            original.run_records,
        )
        store.save(changed)
        assert store.load(original.session_id) == changed

        store.delete(original.session_id)
        with pytest.raises(StoreError):
            store.load(original.session_id)

    def test_list_is_updated_descending_then_session_id_ascending(
        self,
        store: SessionStore,
    ) -> None:
        store.create(make_session("session_z", updated_at=NOW))
        store.create(make_session("session_b", updated_at=NOW + timedelta(minutes=1)))
        store.create(make_session("session_a", updated_at=NOW + timedelta(minutes=1)))

        assert [session.session_id for session in store.list()] == [
            "session_a",
            "session_b",
            "session_z",
        ]

    def test_duplicate_create_fails_without_replacing_existing(
        self,
        store: SessionStore,
    ) -> None:
        original = make_session("session_001")
        store.create(original)

        with pytest.raises(StoreError):
            store.create(make_session("session_001", title="冲突"))

        assert store.load("session_001") == original

    @pytest.mark.parametrize("operation", ["load", "save", "delete"])
    def test_missing_session_operations_fail(
        self,
        store: SessionStore,
        operation: str,
    ) -> None:
        if operation == "save":
            with pytest.raises(StoreError):
                store.save(make_session("missing"))
            return

        if operation == "load":
            with pytest.raises(StoreError):
                store.load("missing")
            return

        with pytest.raises(StoreError):
            store.delete("missing")
