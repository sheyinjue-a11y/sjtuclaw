from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sjtuclaw.infrastructure.tools.downloads import (
    DownloadServiceError,
    DownloadSnapshotService,
)
from tests.fakes import FakeClock, FakeIdGenerator


def test_download_purge_session_is_scoped_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("snapshot", encoding="utf-8")
    service = DownloadSnapshotService(
        root=tmp_path,
        clock=FakeClock(datetime(2026, 7, 20, tzinfo=UTC)),
        ids=FakeIdGenerator(),
    )
    first = service.publish(
        session_id="session_001",
        source=source,
        filename="first.txt",
    )
    second = service.publish(
        session_id="session_002",
        source=source,
        filename="second.txt",
    )

    service.purge_session("session_001")
    service.purge_session("session_001")

    with pytest.raises(DownloadServiceError, match="DOWNLOAD_NOT_FOUND"):
        service.lease(download_id=first.download_id)
    assert service.lease(download_id=second.download_id).filename == "second.txt"
    assert not (tmp_path / "downloads" / first.download_id).exists()
    assert (tmp_path / "downloads" / second.download_id).is_file()


def test_download_lease_rejects_unknown_and_expired_opaque_ids(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("snapshot", encoding="utf-8")
    clock = FakeClock(datetime(2026, 7, 20, tzinfo=UTC))
    service = DownloadSnapshotService(
        root=tmp_path,
        clock=clock,
        ids=FakeIdGenerator(),
        ttl_seconds=30,
    )
    snapshot = service.publish(
        session_id="session_001",
        source=source,
        filename="source.txt",
    )

    assert service.lease(download_id=snapshot.download_id).path.read_bytes() == b"snapshot"
    with pytest.raises(DownloadServiceError, match="DOWNLOAD_NOT_FOUND"):
        service.lease(download_id="https://evil.example/snapshot")

    clock.current += timedelta(seconds=30)

    with pytest.raises(DownloadServiceError, match="DOWNLOAD_NOT_FOUND"):
        service.lease(download_id=snapshot.download_id)
    assert not (tmp_path / "downloads" / snapshot.download_id).exists()
