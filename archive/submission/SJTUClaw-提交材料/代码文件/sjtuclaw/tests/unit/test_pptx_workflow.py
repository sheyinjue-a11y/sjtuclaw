import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "presentations" / "scripts" / "pptx_workflow.py"


def _run(
    script: Path, *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        text=True,
        capture_output=True,
        cwd=cwd,
        env=env,
        check=False,
    )


def test_create_then_inspect_presentation(tmp_path: Path) -> None:
    spec = tmp_path / "deck.json"
    spec.write_text(
        json.dumps(
            {"title": "课程汇报", "slides": [{"title": "问题", "bullets": ["背景", "目标"]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    created = _run(SCRIPT, "create", "--spec", "deck.json", "--output", "deck.pptx", cwd=tmp_path)
    inspected = _run(SCRIPT, "inspect", "--input", "deck.pptx", cwd=tmp_path)

    assert created.returncode == 0, created.stderr
    assert json.loads(inspected.stdout)["slide_count"] == 2
    generated = Presentation(str(tmp_path / "deck.pptx"))
    assert f"{chr(0x2022)} \u80cc\u666f" in generated.slides[1].shapes[1].text


def test_create_requires_slides_but_allows_an_empty_array(tmp_path: Path) -> None:
    missing = tmp_path / "missing-slides.json"
    missing.write_text('{"title":"Deck"}', encoding="utf-8")
    missing_result = _run(
        SCRIPT, "create", "--spec", "missing-slides.json", "--output", "missing.pptx", cwd=tmp_path
    )

    empty = tmp_path / "empty-slides.json"
    empty.write_text('{"title":"Deck","slides":[]}', encoding="utf-8")
    empty_result = _run(
        SCRIPT, "create", "--spec", "empty-slides.json", "--output", "empty.pptx", cwd=tmp_path
    )

    assert missing_result.returncode != 0
    assert "PRESENTATION_SPEC_INVALID" in missing_result.stderr
    assert empty_result.returncode == 0, empty_result.stderr


def test_inspect_rejects_out_of_bounds_shape(tmp_path: Path) -> None:
    source = tmp_path / "out-of-bounds.pptx"
    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6]).shapes.add_textbox(
        presentation.slide_width - Inches(0.5), Inches(1), Inches(1), Inches(1)
    )
    presentation.save(str(source))

    result = _run(SCRIPT, "inspect", "--input", "out-of-bounds.pptx", cwd=tmp_path)

    assert result.returncode != 0
    assert "PRESENTATION_BOUNDS_ERROR" in result.stderr


def test_inspect_rejects_text_overflow(tmp_path: Path) -> None:
    source = tmp_path / "text-overflow.pptx"
    presentation = Presentation()
    textbox = presentation.slides.add_slide(presentation.slide_layouts[6]).shapes.add_textbox(
        Inches(1), Inches(1), Inches(2), Inches(0.35)
    )
    textbox.text_frame.paragraphs[0].text = "overflow " * 100
    textbox.text_frame.paragraphs[0].runs[0].font.size = Pt(16)
    presentation.save(str(source))

    result = _run(SCRIPT, "inspect", "--input", "text-overflow.pptx", cwd=tmp_path)

    assert result.returncode != 0
    assert "PRESENTATION_TEXT_OVERFLOW" in result.stderr


def test_inspect_rejects_just_over_two_lines_of_text(tmp_path: Path) -> None:
    source = tmp_path / "boundary-overflow.pptx"
    presentation = Presentation()
    textbox = presentation.slides.add_slide(presentation.slide_layouts[6]).shapes.add_textbox(
        Inches(1), Inches(1), Inches(2), Inches(0.8)
    )
    textbox.text_frame.paragraphs[0].text = "a" * 30
    textbox.text_frame.paragraphs[0].runs[0].font.size = Pt(16)
    presentation.save(str(source))

    result = _run(SCRIPT, "inspect", "--input", "boundary-overflow.pptx", cwd=tmp_path)

    assert result.returncode != 0
    assert "PRESENTATION_TEXT_OVERFLOW" in result.stderr


def test_inspect_rejects_symlink_that_escapes_workspace(tmp_path: Path) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir()
    source = external / "outside.pptx"
    Presentation().save(str(source))
    link = tmp_path / "escape"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links unavailable in this environment: {error}")

    result = _run(SCRIPT, "inspect", "--input", "escape/outside.pptx", cwd=tmp_path)

    assert result.returncode != 0
    assert "ARTIFACT_INPUT_INVALID" in result.stderr


def test_render_reports_missing_renderer(tmp_path: Path) -> None:
    source = tmp_path / "source.pptx"
    presentation = Presentation()
    presentation.save(str(source))
    environment = dict(os.environ)
    environment["PATH"] = ""

    result = _run(
        SCRIPT,
        "render",
        "--input",
        "source.pptx",
        "--output-dir",
        "pages",
        cwd=tmp_path,
        env=environment,
    )

    assert result.returncode != 0
    assert "PRESENTATION_RENDERER_UNAVAILABLE" in result.stderr
