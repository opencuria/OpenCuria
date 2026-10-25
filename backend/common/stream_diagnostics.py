"""Bounded, sanitized stderr diagnostics shared by workspace stream consumers."""

from __future__ import annotations

import re

#: Keep the newest raw stderr bytes; crash reasons usually appear at the end.
MAX_STDERR_BUFFER_BYTES = 64 * 1024

#: Bound displayed excerpts after sanitizing potentially secret-bearing stderr.
MAX_STDERR_EXCERPT_CHARS = 2000


def sanitize_stderr_excerpt(raw: bytes | bytearray | None) -> str:
    """Return a bounded, secret-free stderr excerpt for logs/skip notes.

    Decoding is lossy (``errors="replace"``) so binary noise cannot
    break logging. Control characters are collapsed to spaces, runs of
    whitespace/newlines are folded, and common secret assignments
    (``token=...``, ``--password ...``) are redacted — stderr of workspace
    processes may echo env or CLI material on crash. The result is capped
    at :data:`MAX_STDERR_EXCERPT_CHARS` chars.
    """
    if not raw:
        return ""
    try:
        text = bytes(raw).decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return ""
    text = "".join(ch if (ch.isprintable() or ch in "\n\t") else " " for ch in text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    text = re.sub(
        r"(?i)(token|secret|password|passwd|pwd|api[_-]?key|auth|bearer"
        r"|credential|private[_-]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(--(?:token|secret|password|api[_-]?key)\s+)\S+",
        r"\1[redacted]",
        text,
    )
    if len(text) > MAX_STDERR_EXCERPT_CHARS:
        text = text[:MAX_STDERR_EXCERPT_CHARS] + "…"
    return text
