"""Attachment JSON Store contracts."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sjtuclaw.errors import AttachmentError
from sjtuclaw.infrastructure.persistence.attachments import JsonAttachmentStore
from sjtuclaw.models.attachment import AttachmentStatus

NOW = datetime(2026, 7, 20, tzinfo=UTC)


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def _pdf_with_text(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        }
    )
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_text_attachment_is_published_with_normalized_text_and_sha256(
    tmp_path: Path,
) -> None:
    store = JsonAttachmentStore(tmp_path)

    attachment = asyncio.run(
        store.ingest_text(
            session_id="session_001",
            attachment_id="attachment_001",
            display_name="notes.txt",
            chunks=_chunks(b"first\r\n", b"second"),
            created_at=NOW,
        )
    )

    assert attachment.status is AttachmentStatus.READY
    assert attachment.size_bytes == 13
    assert attachment.normalized_char_count == 12
    assert attachment.sha256 is not None
    assert len(attachment.sha256) == 64
    assert store.read_normalized("session_001", "attachment_001") == "first\nsecond"
    assert store.list("session_001") == (attachment,)


def test_text_attachment_rejects_cross_session_reads(tmp_path: Path) -> None:
    store = JsonAttachmentStore(tmp_path)
    asyncio.run(
        store.ingest_text(
            session_id="session_001",
            attachment_id="attachment_001",
            display_name="notes.txt",
            chunks=_chunks(b"safe text"),
            created_at=NOW,
        )
    )

    with pytest.raises(AttachmentError, match="ATTACHMENT_SESSION_MISMATCH"):
        store.read_normalized("session_002", "attachment_001")


def test_text_attachment_rejects_stream_larger_than_configured_limit(
    tmp_path: Path,
) -> None:
    store = JsonAttachmentStore(tmp_path, max_file_bytes=4)

    with pytest.raises(AttachmentError, match="ATTACHMENT_LIMIT_EXCEEDED"):
        asyncio.run(
            store.ingest_text(
                session_id="session_001",
                attachment_id="attachment_001",
                display_name="notes.txt",
                chunks=_chunks(b"12345"),
                created_at=NOW,
            )
        )

    assert store.list("session_001") == ()


def test_pdf_attachment_extracts_text_in_a_terminable_helper_process(tmp_path: Path) -> None:
    store = JsonAttachmentStore(tmp_path)

    attachment = asyncio.run(
        store.ingest_pdf(
            session_id="session_001",
            attachment_id="attachment_001",
            display_name="notes.pdf",
            chunks=_chunks(_pdf_with_text("safe PDF text")),
            created_at=NOW,
        )
    )

    assert attachment.kind.value == "pdf"
    assert attachment.status is AttachmentStatus.READY
    assert store.read_normalized("session_001", "attachment_001") == "safe PDF text"


def test_pdf_without_text_is_not_published(tmp_path: Path) -> None:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    store = JsonAttachmentStore(tmp_path)

    with pytest.raises(AttachmentError, match="ATTACHMENT_PDF_NO_TEXT"):
        asyncio.run(
            store.ingest_pdf(
                session_id="session_001",
                attachment_id="attachment_001",
                display_name="blank.pdf",
                chunks=_chunks(output.getvalue()),
                created_at=NOW,
            )
        )

    assert store.list("session_001") == ()


def test_cancelled_upload_cleans_its_staging_directory(tmp_path: Path) -> None:
    asyncio.run(_assert_cancelled_upload_cleans_its_staging_directory(tmp_path))


async def _assert_cancelled_upload_cleans_its_staging_directory(tmp_path: Path) -> None:
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_chunks() -> AsyncIterator[bytes]:
        yield b"started"
        started.set()
        await never.wait()

    store = JsonAttachmentStore(tmp_path)
    task = asyncio.create_task(
        store.ingest_text(
            session_id="session_001",
            attachment_id="attachment_001",
            display_name="notes.txt",
            chunks=blocked_chunks(),
            created_at=NOW,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.list("session_001") == ()
    staging = next((tmp_path / "attachments").glob("*/.staging"), None)
    assert staging is None or not any(staging.iterdir())
