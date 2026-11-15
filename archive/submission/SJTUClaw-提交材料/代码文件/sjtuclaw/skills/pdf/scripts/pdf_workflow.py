"""Bounded workspace-local PDF creation, inspection, filling, and rendering CLI."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


class ArtifactError(Exception):
    """An expected artifact processing error with a stable public code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def workspace_path(raw: str) -> Path:
    root = Path.cwd().resolve()
    candidate = (root / raw).resolve()
    if Path(raw).is_absolute() or not candidate.is_relative_to(root):
        raise ArtifactError("ARTIFACT_INPUT_INVALID", "paths must stay inside the workspace")
    return candidate


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ArtifactError("ARTIFACT_INPUT_INVALID", f"file not found: {path.name}") from error
    except json.JSONDecodeError as error:
        raise ArtifactError("ARTIFACT_INPUT_INVALID", f"invalid JSON: {path.name}") from error
    if not isinstance(data, dict):
        raise ArtifactError("ARTIFACT_INPUT_INVALID", "JSON document must be an object")
    return data


def _string_list(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ArtifactError("ARTIFACT_INPUT_INVALID", f"{name} must be an array of strings")
    return value


def create(spec_path: Path, output_path: Path) -> None:
    spec = _load_json(spec_path)
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ArtifactError("ARTIFACT_INPUT_INVALID", "title must be a non-empty string")
    subtitle = spec.get("subtitle")
    if subtitle is not None and not isinstance(subtitle, str):
        raise ArtifactError("ARTIFACT_INPUT_INVALID", "subtitle must be a string")
    sections = spec.get("sections", [])
    if not isinstance(sections, list) or not all(isinstance(section, dict) for section in sections):
        raise ArtifactError("ARTIFACT_INPUT_INVALID", "sections must be an array of objects")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PdfTitle", parent=styles["Title"], fontName="STSong-Light")
    subtitle_style = ParagraphStyle("PdfSubtitle", parent=styles["Normal"], fontName="STSong-Light")
    heading_style = ParagraphStyle("PdfHeading", parent=styles["Heading2"], fontName="STSong-Light")
    body_style = ParagraphStyle("PdfBody", parent=styles["BodyText"], fontName="STSong-Light", leading=18)
    story: list[Any] = [Paragraph(title, title_style)]
    if subtitle:
        story.extend([Spacer(1, 0.2 * cm), Paragraph(subtitle, subtitle_style)])

    for index, section in enumerate(sections):
        heading = section.get("heading")
        if heading is not None:
            if not isinstance(heading, str):
                raise ArtifactError("ARTIFACT_INPUT_INVALID", f"sections[{index}].heading must be a string")
            story.extend([Spacer(1, 0.35 * cm), Paragraph(heading, heading_style)])
        for paragraph in _string_list(section.get("paragraphs"), f"sections[{index}].paragraphs"):
            story.append(Paragraph(paragraph, body_style))
        bullets = _string_list(section.get("bullets"), f"sections[{index}].bullets")
        if bullets:
            story.append(ListFlowable([ListItem(Paragraph(item, body_style)) for item in bullets], bulletType="bullet"))
        table = section.get("table")
        if table is not None:
            if not isinstance(table, list) or not all(
                isinstance(row, list) and all(isinstance(cell, (str, int, float)) for cell in row)
                for row in table
            ):
                raise ArtifactError("ARTIFACT_INPUT_INVALID", f"sections[{index}].table must be a table")
            if table:
                rendered = Table([[str(cell) for cell in row] for row in table], repeatRows=1)
                rendered.setStyle(
                    TableStyle(
                        [
                            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                            ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                        ]
                    )
                )
                story.extend([Spacer(1, 0.2 * cm), rendered])

    SimpleDocTemplate(str(output_path), pagesize=A4, rightMargin=2 * cm, leftMargin=2 * cm).build(story)


def inspect(input_path: Path) -> dict[str, object]:
    reader = PdfReader(str(input_path))
    if reader.is_encrypted:
        return {"page_count": 0, "encrypted": True, "text_chars": 0, "field_names": []}
    with pdfplumber.open(str(input_path)) as pdf:
        text_chars = sum(len(page.extract_text() or "") for page in pdf.pages)
    return {
        "page_count": len(reader.pages),
        "encrypted": False,
        "text_chars": text_chars,
        "field_names": sorted((reader.get_fields() or {}).keys()),
    }


def fill(input_path: Path, values_path: Path, output_path: Path) -> None:
    values = _load_json(values_path)
    reader = PdfReader(str(input_path))
    if reader.is_encrypted:
        raise ArtifactError("PDF_ENCRYPTED", "encrypted PDFs cannot be filled")
    fields = reader.get_fields() or {}
    missing = sorted(set(values) - set(fields))
    if missing:
        raise ArtifactError("PDF_FIELD_NOT_FOUND", f"fields not found: {', '.join(missing)}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.update_page_form_field_values(None, values, auto_regenerate=False)
    with output_path.open("wb") as stream:
        writer.write(stream)


def render(input_path: Path, output_dir: Path) -> None:
    renderer = shutil.which("pdftoppm")
    if renderer is None:
        raise ArtifactError("PDF_RENDERER_UNAVAILABLE", "pdftoppm was not found on PATH")
    output_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [renderer, "-png", str(input_path), str(output_dir / "page")],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "pdftoppm failed"
        raise ArtifactError("PDF_RENDER_FAILED", detail)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--spec", required=True)
    create_parser.add_argument("--output", required=True)
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--input", required=True)
    fill_parser = commands.add_parser("fill")
    fill_parser.add_argument("--input", required=True)
    fill_parser.add_argument("--values", required=True)
    fill_parser.add_argument("--output", required=True)
    render_parser = commands.add_parser("render")
    render_parser.add_argument("--input", required=True)
    render_parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            create(workspace_path(args.spec), workspace_path(args.output))
        elif args.command == "inspect":
            print(json.dumps(inspect(workspace_path(args.input)), ensure_ascii=False))
        elif args.command == "fill":
            fill(workspace_path(args.input), workspace_path(args.values), workspace_path(args.output))
        elif args.command == "render":
            render(workspace_path(args.input), workspace_path(args.output_dir))
    except ArtifactError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"ARTIFACT_INPUT_INVALID: file not found: {error.filename}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"PDF_PROCESSING_FAILED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
