"""Hard caps for tool output sent to the model provider.

Mirrors OpenCode ``Truncate.output`` (50 KiB / 2000 lines) without
writing the full overflow to disk. Applied as a safety net after every
tool execution so match-count caps (grep/glob) cannot leak huge lines.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_LINES = 2000
MAX_BYTES = 50 * 1024


@dataclass(frozen=True)
class TruncateResult:
    """Clipped tool output plus whether anything was removed."""

    content: str
    truncated: bool


def truncate_tool_output(
    text: str,
    *,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
) -> TruncateResult:
    """Return *text* clipped to *max_lines* and *max_bytes* from the head.

    When clipping is needed a trailing marker records how many lines or
    bytes were omitted. A single oversized line is byte-clipped so the
    model still sees a prefix instead of an empty preview.
    """
    if not text:
        return TruncateResult(content=text, truncated=False)
    lines = text.split("\n")
    total_bytes = len(text.encode("utf-8"))
    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return TruncateResult(content=text, truncated=False)

    kept: list[str] = []
    bytes_used = 0
    hit_bytes = False
    for index, line in enumerate(lines):
        if len(kept) >= max_lines:
            break
        size = len(line.encode("utf-8")) + (1 if index > 0 else 0)
        if bytes_used + size > max_bytes:
            hit_bytes = True
            break
        kept.append(line)
        bytes_used += size

    if not kept and lines:
        first = lines[0].encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        kept = [first]
        bytes_used = len(first.encode("utf-8"))
        hit_bytes = True

    removed = total_bytes - bytes_used if hit_bytes else len(lines) - len(kept)
    unit = "bytes" if hit_bytes else "lines"
    preview = "\n".join(kept)
    marker = f"\n\n...{removed} {unit} truncated..."
    return TruncateResult(content=f"{preview}{marker}", truncated=True)
