from datetime import UTC, datetime

from sjtuclaw.infrastructure.runtime import SystemClock, UuidIdGenerator
from sjtuclaw.ports.runtime import Clock, IdGenerator
from tests.fakes import FakeClock, FakeIdGenerator


def _uses_clock(clock: Clock) -> datetime:
    return clock.now()


def _uses_ids(ids: IdGenerator) -> str:
    return ids.new_id("message")


def test_fake_runtime_primitives_are_deterministic() -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    clock = FakeClock(now)
    ids = FakeIdGenerator()

    assert _uses_clock(clock) == now
    assert _uses_ids(ids) == "message_001"
    assert _uses_ids(ids) == "message_002"


def test_production_runtime_primitives_match_ports() -> None:
    now = _uses_clock(SystemClock())
    generated = _uses_ids(UuidIdGenerator())

    assert now.tzinfo is not None
    assert generated.startswith("message_")
