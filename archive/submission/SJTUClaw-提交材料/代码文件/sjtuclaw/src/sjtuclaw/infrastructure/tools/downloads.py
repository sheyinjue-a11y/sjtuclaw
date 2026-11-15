"""Process-local immutable download snapshots for workspace tool results."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sjtuclaw.errors import SJTUClawError
from sjtuclaw.infrastructure.tools.advanced import DownloadSnapshot
from sjtuclaw.ports.runtime import Clock, IdGenerator


class DownloadServiceError(SJTUClawError):
    """Safe status code for unavailable, expired, or foreign downloads."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DownloadLease:
    """Validated opaque download access; physical paths stay internal."""

    filename: str
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _StoredDownload:
    session_id: str
    snapshot: DownloadSnapshot
    path: Path
    expires_at: datetime


class DownloadSnapshotService:
    """Copy tool-selected files once and serve only the resulting snapshot."""

    def __init__(
        self,
        *,
        root: Path,
        clock: Clock,
        ids: IdGenerator,
        ttl_seconds: int = 600,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("download TTL must be positive")
        self._root = root / "downloads"
        self._clock = clock
        self._ids = ids
        self._ttl = timedelta(seconds=ttl_seconds)
        self._downloads: dict[str, _StoredDownload] = {}
        self._clear_restart_snapshots()

    def publish(
        self,
        *,
        session_id: str,
        source: Path,
        filename: str,
    ) -> DownloadSnapshot:
        self.cleanup()
        if not session_id or not filename or Path(filename).name != filename:
            raise DownloadServiceError("DOWNLOAD_SOURCE_INVALID")
        try:
            before = source.stat()
            if not source.is_file() or source.is_symlink():
                raise DownloadServiceError("DOWNLOAD_SOURCE_NOT_FILE")
        except DownloadServiceError:
            raise
        except OSError as error:
            raise DownloadServiceError("WORKSPACE_UNAVAILABLE") from error

        download_id = self._ids.new_id("download")
        target = self._root / download_id
        temporary: Path | None = None
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as input_stream, tempfile.NamedTemporaryFile(
                mode="xb", prefix=".download-", dir=self._root, delete=False
            ) as output_stream:
                temporary = Path(output_stream.name)
                while chunk := input_stream.read(64 * 1024):
                    digest.update(chunk)
                    size_bytes += len(chunk)
                    output_stream.write(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            after = source.stat()
            if _file_identity(before) != _file_identity(after) or size_bytes != before.st_size:
                raise DownloadServiceError("FILE_CHANGED_SINCE_APPROVAL")
            os.replace(temporary, target)
            temporary = None
        except DownloadServiceError:
            raise
        except OSError as error:
            raise DownloadServiceError("DOWNLOAD_SNAPSHOT_FAILED") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        expires_at = self._clock.now() + self._ttl
        snapshot = DownloadSnapshot(
            download_id=download_id,
            filename=filename,
            size_bytes=size_bytes,
            sha256=digest.hexdigest(),
            expires_at=expires_at.isoformat(),
        )
        self._downloads[download_id] = _StoredDownload(
            session_id=session_id,
            snapshot=snapshot,
            path=target,
            expires_at=expires_at,
        )
        return snapshot

    def lease(self, *, download_id: str) -> DownloadLease:
        self.cleanup()
        stored = self._downloads.get(download_id)
        if stored is None:
            raise DownloadServiceError("DOWNLOAD_NOT_FOUND")
        try:
            stat = stored.path.stat()
            if not stored.path.is_file() or stat.st_size != stored.snapshot.size_bytes:
                raise DownloadServiceError("DOWNLOAD_UNAVAILABLE")
        except DownloadServiceError:
            raise
        except OSError as error:
            raise DownloadServiceError("DOWNLOAD_UNAVAILABLE") from error
        return DownloadLease(
            filename=stored.snapshot.filename,
            path=stored.path,
            size_bytes=stored.snapshot.size_bytes,
            sha256=stored.snapshot.sha256,
        )

    def cleanup(self) -> None:
        now = self._clock.now()
        expired = [
            download_id
            for download_id, stored in self._downloads.items()
            if stored.expires_at <= now
        ]
        for download_id in expired:
            stored = self._downloads.pop(download_id)
            try:
                stored.path.unlink(missing_ok=True)
            except OSError:
                continue

    def purge_session(self, session_id: str) -> None:
        """Delete only snapshots owned by one Session; repeated calls are safe."""

        owned = [
            download_id
            for download_id, stored in self._downloads.items()
            if stored.session_id == session_id
        ]
        for download_id in owned:
            stored = self._downloads.pop(download_id)
            try:
                stored.path.unlink(missing_ok=True)
            except OSError as error:
                self._downloads[download_id] = stored
                raise DownloadServiceError("DOWNLOAD_STORAGE_UNAVAILABLE") from error

    def aclose(self) -> None:
        for stored in tuple(self._downloads.values()):
            try:
                stored.path.unlink(missing_ok=True)
            except OSError:
                continue
        self._downloads.clear()

    def _clear_restart_snapshots(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            for candidate in self._root.iterdir():
                if candidate.is_file() or candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
        except OSError as error:
            raise DownloadServiceError("DOWNLOAD_STORAGE_UNAVAILABLE") from error


def _file_identity(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
