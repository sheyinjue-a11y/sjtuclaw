#!/usr/bin/env python3
"""Extract or validate native visible text in a PowerPoint deck."""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}
SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


def normalize(text: str, collapse_whitespace: bool) -> str:
    text = text.strip()
    if collapse_whitespace:
        return re.sub(r"\s+", " ", text)
    return text


def paragraph_texts(container: ET.Element) -> list[str]:
    paragraphs = []
    for paragraph in container.findall(".//a:p", NS):
        value = "".join(node.text or "" for node in paragraph.findall(".//a:t", NS))
        if value:
            paragraphs.append(value)
    return paragraphs


def extract_entries(pptx: Path, collapse_whitespace: bool) -> list[dict]:
    entries: list[dict] = []
    with zipfile.ZipFile(pptx) as archive:
        slides = []
        for name in archive.namelist():
            match = SLIDE_RE.match(name)
            if match:
                slides.append((int(match.group(1)), name))

        for slide_number, name in sorted(slides):
            root = ET.fromstring(archive.read(name))

            for index, shape in enumerate(root.findall(".//p:sp", NS), start=1):
                paragraphs = paragraph_texts(shape)
                text = normalize("\n".join(paragraphs), collapse_whitespace)
                if text:
                    entries.append(
                        {
                            "slide": slide_number,
                            "kind": "shape",
                            "index": index,
                            "text": text,
                        }
                    )

            for index, cell in enumerate(root.findall(".//a:tc", NS), start=1):
                paragraphs = paragraph_texts(cell)
                text = normalize("\n".join(paragraphs), collapse_whitespace)
                if text:
                    entries.append(
                        {
                            "slide": slide_number,
                            "kind": "table-cell",
                            "index": index,
                            "text": text,
                        }
                    )
    return entries


def write_extract(pptx: Path, out: Path, collapse_whitespace: bool) -> None:
    entries = extract_entries(pptx, collapse_whitespace)
    payload = {
        "schema_version": 1,
        "pptx": str(pptx.resolve()),
        "normalize_whitespace": collapse_whitespace,
        "entries": entries,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Extracted {len(entries)} text containers to {out}")


def expected_counter(manifest: dict, collapse_whitespace: bool) -> Counter:
    expected = Counter()
    for item in manifest.get("expected", []):
        if isinstance(item, str):
            text = item
            count = 1
        else:
            text = item["text"]
            count = int(item.get("count", 1))
        expected[normalize(text, collapse_whitespace)] += count
    return expected


def format_diff(label: str, values: Counter) -> list[str]:
    lines = []
    for text, count in values.items():
        shown = text if len(text) <= 180 else f"{text[:177]}..."
        lines.append(f"{label} x{count}: {shown}")
    return lines


def check(pptx: Path, manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported manifest schema_version; expected 1")

    mode = manifest.get("mode", "strict")
    if mode not in {"strict", "required_subset"}:
        raise ValueError("Unsupported manifest mode; expected strict or required_subset")

    collapse_whitespace = bool(manifest.get("normalize_whitespace", True))
    entries = extract_entries(pptx, collapse_whitespace)
    actual = Counter(entry["text"] for entry in entries)
    expected = expected_counter(manifest, collapse_whitespace)

    missing = expected - actual
    extra = Counter() if mode == "required_subset" else actual - expected
    if not missing and not extra:
        if mode == "required_subset":
            print(
                "Text contract passed: "
                f"{sum(expected.values())} required containers found "
                f"within {sum(actual.values())} total containers"
            )
        else:
            print(f"Text contract passed: {sum(actual.values())} text containers")
        return 0

    print("Text contract failed.", file=sys.stderr)
    for line in format_diff("MISSING", missing):
        print(line, file=sys.stderr)
    for line in format_diff("EXTRA", extra):
        print(line, file=sys.stderr)
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract or validate native text boxes and table cells in a PPTX."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract")
    extract_parser.add_argument("--pptx", type=Path, required=True)
    extract_parser.add_argument("--out", type=Path, required=True)
    extract_parser.add_argument(
        "--preserve-whitespace",
        action="store_true",
        help="Do not collapse whitespace and line breaks.",
    )

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--pptx", type=Path, required=True)
    check_parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "extract":
        write_extract(args.pptx, args.out, not args.preserve_whitespace)
        return 0
    return check(args.pptx, args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
