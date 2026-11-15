from datetime import UTC, datetime

import pytest

from sjtuclaw.models import (
    InvocationSource,
    Message,
    MessageRole,
    RunRecord,
    RunRecordType,
    RuntimeRequest,
    Session,
    UserInput,
)

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def test_session_keeps_stable_message_order() -> None:
    first = Message("msg_001", MessageRole.USER, "你好", NOW, 1)
    second = Message("msg_002", MessageRole.ASSISTANT, "你好", NOW, 2)

    session = Session("session_001", "测试", (first, second), "", NOW, NOW, 3)

    assert [message.message_id for message in session.messages] == ["msg_001", "msg_002"]
    assert session.next_sequence_number == 3


def test_session_rejects_duplicate_message_ids() -> None:
    first = Message("msg_001", MessageRole.USER, "first", NOW, 1)
    duplicate = Message("msg_001", MessageRole.ASSISTANT, "duplicate", NOW, 2)

    with pytest.raises(ValueError, match="message_id values must be unique"):
        Session("session_001", "测试", (first, duplicate), "", NOW, NOW, 3)


@pytest.mark.parametrize("sequence_numbers", [(1, 1), (2, 1)])
def test_session_rejects_non_increasing_message_sequences(
    sequence_numbers: tuple[int, int],
) -> None:
    first = Message("msg_001", MessageRole.USER, "first", NOW, sequence_numbers[0])
    second = Message("msg_002", MessageRole.ASSISTANT, "second", NOW, sequence_numbers[1])

    with pytest.raises(ValueError, match="sequence_number values must be strictly increasing"):
        Session("session_001", "测试", (first, second), "", NOW, NOW, 3)


@pytest.mark.parametrize("next_sequence_number", [1, 2])
def test_session_rejects_next_sequence_number_not_after_messages(
    next_sequence_number: int,
) -> None:
    first = Message("msg_001", MessageRole.USER, "first", NOW, 1)
    second = Message("msg_002", MessageRole.ASSISTANT, "second", NOW, 2)

    with pytest.raises(ValueError, match="must be greater than all message sequence numbers"):
        Session(
            "session_001",
            "测试",
            (first, second),
            "",
            NOW,
            NOW,
            next_sequence_number,
        )


def test_runtime_request_keeps_entrypoint_metadata_out_of_user_input() -> None:
    request = RuntimeRequest(
        request_id="request_001",
        session_id="session_001",
        source=InvocationSource.GATEWAY,
        user_input=UserInput("共享会话消息"),
        actor_id="user_001",
        attachment_refs=("attachment_001",),
    )

    assert request.source is InvocationSource.GATEWAY
    assert request.user_input.text == "共享会话消息"
    assert request.attachment_refs == ("attachment_001",)


def test_models_require_timezone_aware_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Message(
            "msg_001",
            MessageRole.USER,
            "hello",
            datetime(2026, 7, 15, 8, 0),
            1,
        )


def test_session_validates_ordered_unique_run_records() -> None:
    record = RunRecord(
        "record_001",
        "request_001",
        RunRecordType.ERROR,
        "TEST_ERROR",
        "safe message",
        NOW,
        1,
    )

    session = Session("session_001", "title", (), "", NOW, NOW, run_records=(record,))

    assert session.run_records == (record,)
    with pytest.raises(ValueError, match="record_id"):
        Session(
            "session_001",
            "title",
            (),
            "",
            NOW,
            NOW,
            run_records=(record, record),
        )
