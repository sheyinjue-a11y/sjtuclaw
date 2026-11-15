"""Reject secret-looking content before it can become durable memory."""

import re

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:api[ _-]?key|password|secret|token)\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def ensure_safe_memory_content(content: str) -> None:
    if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
        raise ValueError("memory content is invalid")
