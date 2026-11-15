"""Fetch the pinned official DeepSeek-V4 tokenizer artifacts, never model weights."""

from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download
from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError

REPOSITORY = "deepseek-ai/DeepSeek-V4-Flash"
REVISION = "60d8d70770c6776ff598c94bb586a859a38244f1"
DEFAULT_DESTINATION = Path("vendor/deepseek-v4-flash-tokenizer")
ALLOW_PATTERNS = (
    "LICENSE",
    "tokenizer.json",
    "tokenizer_config.json",
    "encoding/README.md",
    "encoding/encoding_dsv4.py",
    "encoding/test_encoding_dsv4.py",
    "encoding/tests/*",
)
REQUIRED_FILES = (
    Path("LICENSE"),
    Path("tokenizer.json"),
    Path("tokenizer_config.json"),
    Path("encoding/encoding_dsv4.py"),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch pinned DeepSeek-V4 tokenizer files without model weights."
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_DESTINATION,
        help=f"local artifact directory (default: {DEFAULT_DESTINATION})",
    )
    args = parser.parse_args()
    destination = args.destination.resolve()
    try:
        snapshot_download(
            repo_id=REPOSITORY,
            revision=REVISION,
            local_dir=destination,
            allow_patterns=list(ALLOW_PATTERNS),
        )
    except (HfHubHTTPError, LocalEntryNotFoundError):
        raise SystemExit(
            "tokenizer download failed; check access to huggingface.co and retry once"
        ) from None

    missing = [str(path) for path in REQUIRED_FILES if not (destination / path).is_file()]
    if missing:
        raise SystemExit(f"download incomplete; missing: {', '.join(missing)}")
    print(f"DeepSeek-V4 tokenizer ready: {destination}")


if __name__ == "__main__":
    main()
