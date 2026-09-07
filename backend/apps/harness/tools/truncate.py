"""Hard caps for tool output sent to the model provider.

Mirrors OpenCode ``Truncate.output`` (50 KiB / 2000 lines) without
writing the full overflow to disk. Applied as a safety net after every
tool execution so match-count caps (grep/glob) cannot leak huge lines.

``direction="tail"`` keeps the *last* lines/bytes because build-log
errors surface at the end; ``"head"`` keeps the first lines/bytes
(default, backwards compatible).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_LINES = 2000
MAX_BYTES = 50 * 1024

TruncateDirection = Literal["head", "tail"]


@dataclass(frozen=True)
class TruncateResult:
    """Clipped tool output plus whether anything was removed."""

    content: str
    truncated: bool


def _head_window(
    lines: list[str], max_lines: int, max_bytes: int
) -> tuple[list[str], int, bool]:
    """Keep the first lines fitting into *max_lines*/*max_bytes*."""
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
    return kept, bytes_used, hit_bytes


def _tail_window(
    lines: list[str], max_lines: int, max_bytes: int
) -> tuple[list[str], int, bool]:
    """Keep the last lines fitting into *max_lines*/*max_bytes*."""
    kept_rev: list[str] = []
    bytes_used = 0
    hit_bytes = False
    for line in reversed(lines):
        if len(kept_rev) >= max_lines:
            break
        size = len(line.encode("utf-8")) + (1 if kept_rev else 0)
        if bytes_used + size > max_bytes:
            hit_bytes = True
            break
        kept_rev.append(line)
        bytes_used += size
    kept_rev.reverse()
    return kept_rev, bytes_used, hit_bytes


def truncate_tool_output(
    text: str,
    *,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
    direction: TruncateDirection = "head",
) -> TruncateResult:
    """Return *text* clipped to *max_lines* and *max_bytes*.

    Args:
        text: Full tool output.
        max_lines: Maximum number of lines kept in the preview.
        max_bytes: Maximum UTF-8 bytes kept in the preview.
        direction: ``"head"`` keeps the first lines/bytes (suffix
            marker), ``"tail"`` keeps the last lines/bytes (prefix
            marker) so trailing build errors stay visible.

    When clipping is needed a marker records how many lines or bytes
    were omitted. A single oversized line is byte-clipped so the model
    still sees a prefix (head) or suffix (tail) instead of nothing.
    """
    if direction not in ("head", "tail"):
        raise ValueError(f"Invalid direction {direction!r}; expected 'head'/'tail'")
    if not text:
        return TruncateResult(content=text, truncated=False)
    lines = text.split("\n")
    total_bytes = len(text.encode("utf-8"))
    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return TruncateResult(content=text, truncated=False)

    if direction == "head":
        kept, bytes_used, hit_bytes = _head_window(lines, max_lines, max_bytes)
        if not kept and lines:
            first = (
                lines[0].encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            )
            kept = [first]
            bytes_used = len(first.encode("utf-8"))
            hit_bytes = True
    else:
        kept, bytes_used, hit_bytes = _tail_window(lines, max_lines, max_bytes)
        if not kept and lines:
            last = (
                lines[-1].encode("utf-8")[-max_bytes:].decode("utf-8", errors="ignore")
            )
            kept = [last]
            bytes_used = len(last.encode("utf-8"))
            hit_bytes = True

    removed = total_bytes - bytes_used if hit_bytes else len(lines) - len(kept)
    unit = "bytes" if hit_bytes else "lines"
    preview = "\n".join(kept)
    if direction == "head":
        marker = f"\n\n...{removed} {unit} truncated..."
        return TruncateResult(content=f"{preview}{marker}", truncated=True)
    marker = f"...{removed} {unit} truncated...\n\n"
    return TruncateResult(content=f"{marker}{preview}", truncated=True)
