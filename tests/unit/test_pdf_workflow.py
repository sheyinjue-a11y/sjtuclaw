import json
import os
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "pdf" / "scripts" / "pdf_workflow.py"


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


def test_create_then_inspect_pdf(tmp_path: Path) -> None:
    spec = tmp_path / "report.json"
    spec.write_text(
        json.dumps(
            {"title": "实验报告", "sections": [{"heading": "结论", "paragraphs": ["可验证内容"]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.pdf"

    created = _run(SCRIPT, "create", "--spec", "report.json", "--output", "report.pdf", cwd=tmp_path)
    inspected = _run(SCRIPT, "inspect", "--input", "report.pdf", cwd=tmp_path)

    assert created.returncode == 0, created.stderr
    assert output.is_file()
    assert inspected.returncode == 0, inspected.stderr
    details = json.loads(inspected.stdout)
    assert details["page_count"] == 1
    assert details["encrypted"] is False
    assert details["text_chars"] > 0
    assert details["field_names"] == []


def test_fill_updates_acroform_text_field(tmp_path: Path) -> None:
    source = tmp_path / "form.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.acroForm.textfield(name="student_name", x=72, y=720, width=180, height=20)
    pdf.showPage()
    pdf.save()
    values = tmp_path / "values.json"
    values.write_text('{"student_name":"Ada"}', encoding="utf-8")

    filled = _run(
        SCRIPT,
        "fill",
        "--input",
        "form.pdf",
        "--values",
        "values.json",
        "--output",
        "filled.pdf",
        cwd=tmp_path,
    )
    inspected = _run(SCRIPT, "inspect", "--input", "filled.pdf", cwd=tmp_path)

    assert filled.returncode == 0, filled.stderr
    assert json.loads(inspected.stdout)["field_names"] == ["student_name"]
    fields = PdfReader(str(tmp_path / "filled.pdf")).get_fields()
    assert fields is not None
    assert fields["student_name"]["/V"] == "Ada"


def test_fill_rejects_missing_field(tmp_path: Path) -> None:
    source = tmp_path / "form.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.acroForm.textfield(name="student_name", x=72, y=720, width=180, height=20)
    pdf.showPage()
    pdf.save()
    values = tmp_path / "values.json"
    values.write_text('{"absent":"Ada"}', encoding="utf-8")

    result = _run(
        SCRIPT,
        "fill",
        "--input",
        "form.pdf",
        "--values",
        "values.json",
        "--output",
        "filled.pdf",
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "PDF_FIELD_NOT_FOUND" in result.stderr


def test_render_reports_missing_renderer(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(72, 720, "sample")
    pdf.save()
    environment = dict(os.environ)
    environment["PATH"] = ""

    result = _run(
        SCRIPT,
        "render",
        "--input",
        "source.pdf",
        "--output-dir",
        "pages",
        cwd=tmp_path,
        env=environment,
    )

    assert result.returncode != 0
    assert "PDF_RENDERER_UNAVAILABLE" in result.stderr


def test_cli_rejects_absolute_and_traversal_paths(tmp_path: Path) -> None:
    absolute = _run(SCRIPT, "inspect", "--input", str(tmp_path / "source.pdf"), cwd=tmp_path)
    traversal = _run(SCRIPT, "inspect", "--input", "../source.pdf", cwd=tmp_path)

    assert absolute.returncode != 0
    assert "ARTIFACT_INPUT_INVALID" in absolute.stderr
    assert traversal.returncode != 0
    assert "ARTIFACT_INPUT_INVALID" in traversal.stderr


def test_fill_rejects_encrypted_acroform_pdf(tmp_path: Path) -> None:
    encrypted = tmp_path / "encrypted.pdf"
    pdf = canvas.Canvas(str(encrypted))
    pdf.setEncrypt("secret")
    pdf.acroForm.textfield(name="student_name", x=72, y=720, width=180, height=20)
    pdf.showPage()
    pdf.save()
    values = tmp_path / "values.json"
    values.write_text('{"student_name":"Ada"}', encoding="utf-8")

    result = _run(
        SCRIPT,
        "fill",
        "--input",
        "encrypted.pdf",
        "--values",
        "values.json",
        "--output",
        "filled.pdf",
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "PDF_ENCRYPTED" in result.stderr
