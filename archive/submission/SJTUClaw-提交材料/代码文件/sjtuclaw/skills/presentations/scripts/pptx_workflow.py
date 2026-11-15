"""Create, inspect, and render small workspace-local PowerPoint presentations."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.exc import PackageNotFoundError
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

BACKGROUND = RGBColor(250, 250, 248)
TEXT = RGBColor(31, 41, 55)
ACCENT = RGBColor(22, 78, 99)
FONT_NAME = "Microsoft YaHei"
EMU_PER_POINT = 12700
DEFAULT_TEXT_SIZE_PT = 18
LINE_HEIGHT_MULTIPLIER = 1.35


class WorkflowError(Exception):
    """An error whose code is safe for CLI callers to inspect."""


def _fail(code: str, message: str) -> NoReturn:
    raise WorkflowError(f"{code}: {message}")


def _workspace_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        _fail("ARTIFACT_INPUT_INVALID", "paths must be relative to the current workspace")
    workspace = Path.cwd().resolve()
    candidate = (workspace / path).resolve()
    if not candidate.is_relative_to(workspace):
        _fail("ARTIFACT_INPUT_INVALID", "paths must resolve inside the current workspace")
    return candidate


def _set_run_font(run: object, size: int, bold: bool = False, color: RGBColor = TEXT) -> None:
    font = run.font  # type: ignore[attr-defined]
    font.name = FONT_NAME
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = color
    run._r.get_or_add_rPr().set(qn("a:ea"), FONT_NAME)  # type: ignore[attr-defined]


def _add_textbox(slide: object, text: str, left: float, top: float, width: float, height: float,
                 size: int, bold: bool = False, color: RGBColor = TEXT) -> object:
    text_box = slide.shapes.add_textbox(left, top, width, height)  # type: ignore[attr-defined]
    frame = text_box.text_frame
    frame.clear()
    paragraph = frame.paragraphs[0]
    paragraph.alignment = PP_ALIGN.LEFT
    run = paragraph.add_run()
    run.text = text
    _set_run_font(run, size, bold, color)
    return text_box


def _expect_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("PRESENTATION_SPEC_INVALID", f"{field} must be a non-empty string")
    return value.strip()


def _expect_slides(value: object) -> Sequence[Mapping[str, object]]:
    if not isinstance(value, list):
        _fail("PRESENTATION_SPEC_INVALID", "slides must be an array")
    slides: list[Mapping[str, object]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            _fail("PRESENTATION_SPEC_INVALID", f"slides[{index}] must be an object")
        slides.append(item)
    return slides


def _text_units(text: str) -> float:
    """Estimate line width in ems, conservatively weighting wide characters."""
    return sum(1.0 if unicodedata.east_asian_width(character) in "WF" else 0.56 for character in text)


def _paragraph_line_count(paragraph: object, available_width_pt: float) -> tuple[int, float]:
    """Estimate wrapped visual lines using every run's font size and content."""
    runs = paragraph.runs  # type: ignore[attr-defined]
    if not runs:
        return 1, float(DEFAULT_TEXT_SIZE_PT)
    line_count = 1
    line_width = 0.0
    largest_size = float(DEFAULT_TEXT_SIZE_PT)
    for run in runs:
        size = run.font.size.pt if run.font.size is not None else DEFAULT_TEXT_SIZE_PT
        largest_size = max(largest_size, float(size))
        maximum_units = max(1.0, available_width_pt / float(size))
        fragments = run.text.split("\n")
        for fragment_index, fragment in enumerate(fragments):
            fragment_units = _text_units(fragment)
            total_units = line_width + fragment_units
            required_lines = max(1, math.ceil(total_units / maximum_units))
            line_count += required_lines - 1
            line_width = total_units - (required_lines - 1) * maximum_units
            if fragment_index < len(fragments) - 1:
                line_count += 1
                line_width = 0.0
    return line_count, largest_size


def _text_overflows(shape: object) -> bool:
    """Return whether text conservatively exceeds a textbox's usable height."""
    if not shape.has_text_frame or not shape.text.strip():  # type: ignore[attr-defined]
        return False
    frame = shape.text_frame  # type: ignore[attr-defined]
    available_width_pt = (shape.width - frame.margin_left - frame.margin_right) / EMU_PER_POINT  # type: ignore[attr-defined]
    available_height_pt = (shape.height - frame.margin_top - frame.margin_bottom) / EMU_PER_POINT  # type: ignore[attr-defined]
    if available_width_pt <= 0 or available_height_pt <= 0:
        return True
    required_height_pt = 0.0
    for paragraph in frame.paragraphs:
        lines, size = _paragraph_line_count(paragraph, available_width_pt)
        required_height_pt += lines * size * LINE_HEIGHT_MULTIPLIER
        required_height_pt += paragraph.space_before.pt if paragraph.space_before else 0
        required_height_pt += paragraph.space_after.pt if paragraph.space_after else 0
    return required_height_pt > available_height_pt


def create_presentation(spec: Mapping[str, object], output: Path) -> None:
    """Create a 16:9 deck from a compact JSON-compatible specification."""
    title = _expect_string(spec.get("title"), "title")
    slides = _expect_slides(spec.get("slides"))
    presentation = Presentation()
    presentation.slide_width = Inches(13.333333)
    presentation.slide_height = Inches(7.5)

    title_slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    title_slide.background.fill.solid()
    title_slide.background.fill.fore_color.rgb = BACKGROUND
    _add_textbox(title_slide, title, Inches(1.05), Inches(2.55), Inches(11.2), Inches(1.2), 50, True, ACCENT)

    for index, item in enumerate(slides, start=1):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = BACKGROUND
        slide_title = _expect_string(item.get("title"), f"slides[{index}].title")
        _add_textbox(slide, slide_title, Inches(0.75), Inches(0.4), Inches(11.8), Inches(0.9), 35, True, ACCENT)

        body = item.get("body")
        bullets = item.get("bullets")
        body_lines: list[str] = []
        if body is not None:
            body_lines.append(_expect_string(body, f"slides[{index}].body"))
        if bullets is not None:
            if not isinstance(bullets, list) or not all(isinstance(line, str) and line.strip() for line in bullets):
                _fail("PRESENTATION_SPEC_INVALID", f"slides[{index}].bullets must be a string array")
            body_lines.extend(f"{chr(0x2022)} {line.strip()}" for line in bullets)
        if body_lines:
            box = _add_textbox(slide, body_lines[0], Inches(0.95), Inches(1.55), Inches(6.3), Inches(4.9), 16)
            frame = box.text_frame
            for line in body_lines[1:]:
                paragraph = frame.add_paragraph()
                paragraph.space_before = Pt(10)
                run = paragraph.add_run()
                run.text = line
                _set_run_font(run, 16)

        image = item.get("image")
        if image is not None:
            image_path = _workspace_path(_expect_string(image, f"slides[{index}].image"))
            if not image_path.is_file():
                _fail("PRESENTATION_IMAGE_NOT_FOUND", f"slides[{index}].image does not exist")
            slide.shapes.add_picture(str(image_path), Inches(7.55), Inches(1.55), width=Inches(4.75), height=Inches(4.8))

    output.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(output)


def inspect_presentation(source: Path) -> dict[str, object]:
    """Return structural metadata and reject shapes that leave the slide canvas."""
    if not source.is_file():
        _fail("PRESENTATION_INPUT_NOT_FOUND", "input presentation does not exist")
    try:
        presentation = Presentation(source)
    except (OSError, PackageNotFoundError, ValueError, zipfile.BadZipFile) as error:
        _fail("PRESENTATION_INPUT_INVALID", str(error))
    for slide_number, slide in enumerate(presentation.slides, start=1):
        for shape_number, shape in enumerate(slide.shapes, start=1):
            if shape.left < 0 or shape.top < 0 or shape.left + shape.width > presentation.slide_width or shape.top + shape.height > presentation.slide_height:
                _fail("PRESENTATION_BOUNDS_ERROR", f"slide {slide_number}, shape {shape_number} exceeds slide bounds")
            if _text_overflows(shape):
                _fail("PRESENTATION_TEXT_OVERFLOW", f"slide {slide_number}, shape {shape_number} text exceeds its frame")
    return {
        "slide_count": len(presentation.slides),
        "slide_width": presentation.slide_width,
        "slide_height": presentation.slide_height,
    }


def render_presentation(source: Path, output_dir: Path) -> None:
    """Render via LibreOffice PDF conversion and Poppler rasterization."""
    soffice = shutil.which("soffice")
    pdftoppm = shutil.which("pdftoppm")
    if not soffice or not pdftoppm:
        _fail(
            "PRESENTATION_RENDERER_UNAVAILABLE",
            "render requires both soffice and pdftoppm on PATH; install LibreOffice and Poppler, then retry",
        )
    if not source.is_file():
        _fail("PRESENTATION_INPUT_NOT_FOUND", "input presentation does not exist")

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pptx-render-") as temporary_directory:
        temporary = Path(temporary_directory)
        converted = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(temporary), str(source)],
            text=True,
            capture_output=True,
            check=False,
        )
        pdf = temporary / f"{source.stem}.pdf"
        if converted.returncode != 0 or not pdf.is_file():
            _fail("PRESENTATION_RENDER_FAILED", converted.stderr.strip() or "LibreOffice did not produce a PDF")
        rasterized = subprocess.run(
            [pdftoppm, "-png", str(pdf), str(output_dir / "slide")],
            text=True,
            capture_output=True,
            check=False,
        )
        if rasterized.returncode != 0:
            _fail("PRESENTATION_RENDER_FAILED", rasterized.stderr.strip() or "pdftoppm failed")


def _load_spec(path: Path) -> Mapping[str, object]:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _fail("PRESENTATION_SPEC_INVALID", str(error))
    if not isinstance(content, Mapping):
        _fail("PRESENTATION_SPEC_INVALID", "spec root must be an object")
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create")
    create.add_argument("--spec", required=True)
    create.add_argument("--output", required=True)
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("--input", required=True)
    render = subcommands.add_parser("render")
    render.add_argument("--input", required=True)
    render.add_argument("--output-dir", required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            create_presentation(_load_spec(_workspace_path(arguments.spec)), _workspace_path(arguments.output))
        elif arguments.command == "inspect":
            print(json.dumps(inspect_presentation(_workspace_path(arguments.input)), ensure_ascii=False))
        else:
            render_presentation(_workspace_path(arguments.input), _workspace_path(arguments.output_dir))
    except WorkflowError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
