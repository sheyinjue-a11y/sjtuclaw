"""Strict local JSON Store for ready UTF-8 text attachments."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from collections.abc import AsyncIterable
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from sjtuclaw.errors import AttachmentError
from sjtuclaw.models.attachment import (
    Attachment,
    AttachmentKind,
    AttachmentRead,
    AttachmentStatus,
)

_SCHEMA_VERSION = 1
_METADATA_FIELDS = frozenset(
    {
        "schemaVersion",
        "attachmentId",
        "sessionId",
        "displayName",
        "kind",
        "status",
        "createdAt",
        "updatedAt",
        "sizeBytes",
        "sha256",
        "normalizedCharCount",
        "errorCode",
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
MAX_ATTACHMENT_READ_CHARS = 32_000
DEFAULT_PDF_MAX_MEMORY_BYTES = 64 * 1024 * 1024
_PDF_FAILURE_CODES = frozenset(
    {
        "ATTACHMENT_LIMIT_EXCEEDED",
        "ATTACHMENT_PDF_ENCRYPTED",
        "ATTACHMENT_PDF_NO_TEXT",
        "ATTACHMENT_PDF_INVALID",
    }
)
_PDF_EXTRACT_SCRIPT = """
import sys
from pypdf import PdfReader

path, raw_page_limit, raw_char_limit, raw_memory_limit = sys.argv[1:]
page_limit = int(raw_page_limit)
char_limit = int(raw_char_limit)
memory_limit = int(raw_memory_limit)

def fail(code):
    sys.stderr.write(code)
    raise SystemExit(2)

def limit_memory(limit):
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return
    except Exception:
        pass
    if sys.platform != "win32":
        fail("ATTACHMENT_PROCESS_FAILED")
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error())
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x100
        info.ProcessMemoryLimit = limit
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            raise OSError(ctypes.get_last_error())
        globals()["_job"] = job
    except Exception:
        fail("ATTACHMENT_PROCESS_FAILED")

try:
    limit_memory(memory_limit)
    reader = PdfReader(path)
    if reader.is_encrypted:
        fail("ATTACHMENT_PDF_ENCRYPTED")
    if len(reader.pages) > page_limit:
        fail("ATTACHMENT_LIMIT_EXCEEDED")
    parts = []
    count = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        count += len(text)
        if count > char_limit:
            fail("ATTACHMENT_LIMIT_EXCEEDED")
        parts.append(text)
    normalized = "\\n".join(parts)
    if not normalized.strip():
        fail("ATTACHMENT_PDF_NO_TEXT")
    sys.stdout.buffer.write(normalized.encode("utf-8"))
except SystemExit:
    raise
except Exception:
    fail("ATTACHMENT_PDF_INVALID")
"""


class JsonAttachmentStore:
    """Publish original and normalized text together by atomic directory replace."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 20 * 1024 * 1024,
        max_session_bytes: int = 200 * 1024 * 1024,
        max_normalized_chars: int = 2_000_000,
        pdf_max_pages: int = 300,
        pdf_timeout_seconds: int = 60,
        pdf_max_memory_bytes: int = DEFAULT_PDF_MAX_MEMORY_BYTES,
    ) -> None:
        if min(
            max_file_bytes,
            max_session_bytes,
            max_normalized_chars,
            pdf_max_pages,
            pdf_timeout_seconds,
            pdf_max_memory_bytes,
        ) < 1:
            raise ValueError("attachment limits must be positive")
        self._root = root / "attachments"
        self._max_file_bytes = max_file_bytes
        self._max_session_bytes = max_session_bytes
        self._max_normalized_chars = max_normalized_chars
        self._pdf_max_pages = pdf_max_pages
        self._pdf_timeout_seconds = pdf_timeout_seconds
        self._pdf_max_memory_bytes = pdf_max_memory_bytes
        self._cursors: dict[str, tuple[str, str, int]] = {}

    async def ingest_text(
        self,
        *,
        session_id: str,
        attachment_id: str,
        display_name: str,
        chunks: AsyncIterable[bytes],
        created_at: datetime,
    ) -> Attachment:
        self._validate_ids(session_id, attachment_id)
        self._validate_display_name(display_name)
        session_dir = self._session_dir(session_id)
        final_dir = session_dir / attachment_id
        if final_dir.exists():
            raise AttachmentError("ATTACHMENT_ALREADY_EXISTS")
        staging_dir = session_dir / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=".upload-", dir=staging_dir))
        original = stage_dir / "original"
        normalized = stage_dir / "normalized.txt"
        digest = hashlib.sha256()
        size_bytes = 0
        char_count = 0
        pending_carriage_return = False
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            with original.open("wb") as original_handle, normalized.open(
                "w",
                encoding="utf-8",
                newline="",
            ) as normalized_handle:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise AttachmentError("ATTACHMENT_PROCESS_FAILED")
                    size_bytes += len(chunk)
                    if size_bytes > self._max_file_bytes:
                        raise AttachmentError("ATTACHMENT_LIMIT_EXCEEDED")
                    digest.update(chunk)
                    original_handle.write(chunk)
                    text = decoder.decode(chunk)
                    text, pending_carriage_return = _normalize_newlines(
                        text,
                        pending_carriage_return,
                    )
                    char_count += len(text)
                    if char_count > self._max_normalized_chars:
                        raise AttachmentError("ATTACHMENT_LIMIT_EXCEEDED")
                    normalized_handle.write(text)
                tail = decoder.decode(b"", final=True)
                tail, pending_carriage_return = _normalize_newlines(
                    tail,
                    pending_carriage_return,
                )
                if pending_carriage_return:
                    tail += "\n"
                char_count += len(tail)
                if char_count > self._max_normalized_chars:
                    raise AttachmentError("ATTACHMENT_LIMIT_EXCEEDED")
                normalized_handle.write(tail)
                original_handle.flush()
                os.fsync(original_handle.fileno())
                normalized_handle.flush()
                os.fsync(normalized_handle.fileno())
        except asyncio.CancelledError:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            raise
        except AttachmentError:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            raise
        except UnicodeDecodeError as exc:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            raise AttachmentError("ATTACHMENT_NOT_UTF8") from exc
        except OSError as exc:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
        try:
            if self._ready_size_bytes(session_id) + size_bytes > self._max_session_bytes:
                raise AttachmentError("ATTACHMENT_LIMIT_EXCEEDED")
            attachment = Attachment(
                attachment_id=attachment_id,
                session_id=session_id,
                display_name=display_name,
                kind=AttachmentKind.TEXT,
                status=AttachmentStatus.READY,
                created_at=created_at,
                updated_at=created_at,
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
                normalized_char_count=char_count,
            )
            self._write_metadata(stage_dir / "metadata.json", attachment)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage_dir, final_dir)
            return attachment
        except OSError as exc:
            raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)

    async def ingest_pdf(
        self,
        *,
        session_id: str,
        attachment_id: str,
        display_name: str,
        chunks: AsyncIterable[bytes],
        created_at: datetime,
    ) -> Attachment:
        self._validate_ids(session_id, attachment_id)
        self._validate_display_name(display_name)
        session_dir = self._session_dir(session_id)
        final_dir = session_dir / attachment_id
        if final_dir.exists():
            raise AttachmentError("ATTACHMENT_ALREADY_EXISTS")
        staging_dir = session_dir / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=".upload-", dir=staging_dir))
        original = stage_dir / "original"
        normalized = stage_dir / "normalized.txt"
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            try:
                with original.open("wb") as original_handle:
                    async for chunk in chunks:
                        if not isinstance(chunk, bytes):
                            raise AttachmentError("ATTACHMENT_PROCESS_FAILED")
                        size_bytes += len(chunk)
                        if size_bytes > self._max_file_bytes:
                            raise AttachmentError("ATTACHMENT_LIMIT_EXCEEDED")
                        digest.update(chunk)
                        original_handle.write(chunk)
                    original_handle.flush()
                    os.fsync(original_handle.fileno())
            except OSError as exc:
                raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
            extracted = await _extract_pdf_text(
                original,
                max_pages=self._pdf_max_pages,
                max_chars=self._max_normalized_chars,
                timeout_seconds=self._pdf_timeout_seconds,
                max_memory_bytes=self._pdf_max_memory_bytes,
            )
            normalized_text, pending_carriage_return = _normalize_newlines(extracted, False)
            if pending_carriage_return:
                normalized_text += "\n"
            if len(normalized_text) > self._max_normalized_chars:
                raise AttachmentError("ATTACHMENT_LIMIT_EXCEEDED")
            try:
                with normalized.open("w", encoding="utf-8", newline="") as normalized_handle:
                    normalized_handle.write(normalized_text)
                    normalized_handle.flush()
                    os.fsync(normalized_handle.fileno())
            except OSError as exc:
                raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
            if self._ready_size_bytes(session_id) + size_bytes > self._max_session_bytes:
                raise AttachmentError("ATTACHMENT_LIMIT_EXCEEDED")
            attachment = Attachment(
                attachment_id=attachment_id,
                session_id=session_id,
                display_name=display_name,
                kind=AttachmentKind.PDF,
                status=AttachmentStatus.READY,
                created_at=created_at,
                updated_at=created_at,
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
                normalized_char_count=len(normalized_text),
            )
            self._write_metadata(stage_dir / "metadata.json", attachment)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage_dir, final_dir)
            return attachment
        except OSError as exc:
            raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)

    def list(self, session_id: str) -> tuple[Attachment, ...]:
        self._validate_component(session_id)
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return ()
        attachments = [
            self._read_metadata(child / "metadata.json")
            for child in session_dir.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        ]
        return tuple(sorted(attachments, key=lambda item: (item.created_at, item.attachment_id)))

    def load(self, session_id: str, attachment_id: str) -> Attachment:
        self._validate_ids(session_id, attachment_id)
        metadata_path = self._session_dir(session_id) / attachment_id / "metadata.json"
        if not metadata_path.is_file():
            if self._exists_in_another_session(session_id, attachment_id):
                raise AttachmentError("ATTACHMENT_SESSION_MISMATCH")
            raise AttachmentError("ATTACHMENT_NOT_FOUND")
        attachment = self._read_metadata(metadata_path)
        if attachment.session_id != session_id or attachment.attachment_id != attachment_id:
            raise AttachmentError("ATTACHMENT_CORRUPT")
        return attachment

    def read_normalized(self, session_id: str, attachment_id: str) -> str:
        attachment = self.load(session_id, attachment_id)
        if attachment.status is not AttachmentStatus.READY:
            raise AttachmentError("ATTACHMENT_NOT_READY")
        path = self._session_dir(session_id) / attachment_id / "normalized.txt"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise AttachmentError("ATTACHMENT_CORRUPT") from exc
        except (OSError, UnicodeError) as exc:
            raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc

    def open_original(self, session_id: str, attachment_id: str) -> BinaryIO:
        attachment = self.load(session_id, attachment_id)
        if attachment.status is not AttachmentStatus.READY:
            raise AttachmentError("ATTACHMENT_NOT_READY")
        path = self._session_dir(session_id) / attachment_id / "original"
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise AttachmentError("ATTACHMENT_CORRUPT") from exc
        except OSError as exc:
            raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc

    def read_slice(
        self,
        session_id: str,
        attachment_id: str,
        *,
        cursor: str | None,
        max_chars: int,
    ) -> AttachmentRead:
        if not 1 <= max_chars <= MAX_ATTACHMENT_READ_CHARS:
            raise AttachmentError("ATTACHMENT_READ_LIMIT_EXCEEDED")
        attachment = self.load(session_id, attachment_id)
        if attachment.status is not AttachmentStatus.READY:
            raise AttachmentError("ATTACHMENT_NOT_READY")
        offset = self._decode_cursor(session_id, attachment_id, cursor)
        path = self._session_dir(session_id) / attachment_id / "normalized.txt"
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                handle.read(offset)
                content = handle.read(max_chars)
                has_more = handle.read(1) != ""
        except FileNotFoundError as exc:
            raise AttachmentError("ATTACHMENT_CORRUPT") from exc
        except (OSError, UnicodeError) as exc:
            raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
        next_cursor = (
            self._new_cursor(session_id, attachment_id, offset + len(content))
            if has_more
            else None
        )
        return AttachmentRead(content=content, next_cursor=next_cursor)

    def require_ready(
        self,
        session_id: str,
        attachment_refs: tuple[str, ...],
    ) -> tuple[Attachment, ...]:
        if len(attachment_refs) > 10 or len(attachment_refs) != len(set(attachment_refs)):
            raise AttachmentError("ATTACHMENT_LIMIT_EXCEEDED")
        attachments = tuple(
            self.load(session_id, attachment_id) for attachment_id in attachment_refs
        )
        if any(attachment.status is not AttachmentStatus.READY for attachment in attachments):
            raise AttachmentError("ATTACHMENT_NOT_READY")
        return attachments

    def delete(self, session_id: str, attachment_id: str) -> None:
        self.load(session_id, attachment_id)
        path = self._session_dir(session_id) / attachment_id
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
        self._cursors = {
            cursor: value
            for cursor, value in self._cursors.items()
            if value[:2] != (session_id, attachment_id)
        }

    def _ready_size_bytes(self, session_id: str) -> int:
        return sum(
            attachment.size_bytes or 0
            for attachment in self.list(session_id)
            if attachment.status is AttachmentStatus.READY
        )

    def _session_dir(self, session_id: str) -> Path:
        return self._root / hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _exists_in_another_session(self, session_id: str, attachment_id: str) -> bool:
        if not self._root.exists():
            return False
        current = self._session_dir(session_id)
        return any(
            child != current and (child / attachment_id / "metadata.json").is_file()
            for child in self._root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        )

    def _new_cursor(self, session_id: str, attachment_id: str, offset: int) -> str:
        cursor = secrets.token_urlsafe(24)
        self._cursors[cursor] = (session_id, attachment_id, offset)
        return cursor

    def _decode_cursor(
        self,
        session_id: str,
        attachment_id: str,
        cursor: str | None,
    ) -> int:
        if cursor is None:
            return 0
        value = self._cursors.get(cursor)
        if value is None or value[:2] != (session_id, attachment_id):
            raise AttachmentError("ATTACHMENT_CURSOR_INVALID")
        return value[2]

    @staticmethod
    def _validate_component(value: str) -> None:
        if _SAFE_ID.fullmatch(value) is None:
            raise AttachmentError("ATTACHMENT_NOT_FOUND")

    def _validate_ids(self, session_id: str, attachment_id: str) -> None:
        self._validate_component(session_id)
        self._validate_component(attachment_id)

    @staticmethod
    def _validate_display_name(display_name: str) -> None:
        if (
            not display_name
            or len(display_name) > 255
            or any(character in display_name for character in ("/", "\\", "\0"))
        ):
            raise AttachmentError("ATTACHMENT_INVALID_NAME")

    @staticmethod
    def _write_metadata(path: Path, attachment: Attachment) -> None:
        document = {
            "schemaVersion": _SCHEMA_VERSION,
            "attachmentId": attachment.attachment_id,
            "sessionId": attachment.session_id,
            "displayName": attachment.display_name,
            "kind": attachment.kind.value,
            "status": attachment.status.value,
            "createdAt": attachment.created_at.isoformat(),
            "updatedAt": attachment.updated_at.isoformat(),
            "sizeBytes": attachment.size_bytes,
            "sha256": attachment.sha256,
            "normalizedCharCount": attachment.normalized_char_count,
            "errorCode": attachment.error_code,
        }
        path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _read_metadata(path: Path) -> Attachment:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or frozenset(data) != _METADATA_FIELDS:
                raise AttachmentError("ATTACHMENT_CORRUPT")
            if data["schemaVersion"] != _SCHEMA_VERSION:
                raise AttachmentError("ATTACHMENT_CORRUPT")
            return Attachment(
                attachment_id=_required_string(data, "attachmentId"),
                session_id=_required_string(data, "sessionId"),
                display_name=_required_string(data, "displayName"),
                kind=AttachmentKind(_required_string(data, "kind")),
                status=AttachmentStatus(_required_string(data, "status")),
                created_at=_timestamp(data, "createdAt"),
                updated_at=_timestamp(data, "updatedAt"),
                size_bytes=_optional_int(data, "sizeBytes"),
                sha256=_optional_str(data, "sha256"),
                normalized_char_count=_optional_int(data, "normalizedCharCount"),
                error_code=_optional_str(data, "errorCode"),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AttachmentError("ATTACHMENT_CORRUPT") from exc


def _normalize_newlines(value: str, pending_carriage_return: bool) -> tuple[str, bool]:
    output: list[str] = []
    for character in value:
        if pending_carriage_return:
            output.append("\n")
            pending_carriage_return = False
            if character == "\n":
                continue
        if character == "\r":
            pending_carriage_return = True
        else:
            output.append(character)
    return "".join(output), pending_carriage_return


def _required_string(data: dict[str, object], key: str) -> str:
    value = data[key]
    if type(value) is not str:
        raise ValueError(key)
    return value


def _optional_str(data: dict[str, object], key: str) -> str | None:
    value = data[key]
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(key)
    return value


def _optional_int(data: dict[str, object], key: str) -> int | None:
    value = data[key]
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(key)
    return value


def _timestamp(data: dict[str, object], key: str) -> datetime:
    value = _required_string(data, key)
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(key)
    return timestamp


async def _extract_pdf_text(
    path: Path,
    *,
    max_pages: int,
    max_chars: int,
    timeout_seconds: int,
    max_memory_bytes: int,
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _PDF_EXTRACT_SCRIPT,
            str(path),
            str(max_pages),
            str(max_chars),
            str(max_memory_bytes),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AttachmentError("ATTACHMENT_PROCESS_TIMEOUT") from exc
    if process.returncode != 0:
        code = stderr.decode("ascii", errors="ignore").strip()
        raise AttachmentError(
            code if code in _PDF_FAILURE_CODES else "ATTACHMENT_PROCESS_FAILED"
        )
    try:
        return stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AttachmentError("ATTACHMENT_PROCESS_FAILED") from exc
