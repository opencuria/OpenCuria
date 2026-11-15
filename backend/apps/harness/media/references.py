"""Parse workspace media references without reading files or loading engines."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..access.base import sanitize_harness_path

#: Any markdown image; workspace vs remote is decided after dest parsing.
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

#: Hydration is image-only (png/jpeg/gif/webp). Videos stay frontend-only.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_REMOTE_DEST_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
_FILE_REFERENCE = re.compile(r'(?<![\w@])@file:(/workspace/[^\s<>"`\\]+)')
_MEDIA_EXTENSIONS = (*_IMAGE_EXTENSIONS, ".pdf")


@dataclass(frozen=True)
class MediaReference:
    """A user-authored media reference and its exact source span."""

    start: int
    end: int
    path: str
    kind: str
    markdown: bool


def parse_markdown_link_dest(raw: str) -> str:
    """Return the destination path from a markdown link/image dest.

    Strips ``<...>`` wrapping and an optional title (``"..."``, ``'...'``,
    or parenthesized) so ``foo.png "caption"`` becomes ``foo.png``.
    """
    dest = (raw or "").strip()
    if dest.startswith("<"):
        close = dest.find(">")
        if close >= 0:
            return dest[1:close].strip()
        dest = dest[1:].lstrip()
    for index, char in enumerate(dest):
        if char.isspace():
            return dest[:index]
    return dest


def resolve_workspace_image_path(raw_dest: str) -> str | None:
    """Return a sandboxed ``/workspace/...`` image path, or None.

    Relative dests resolve against ``/workspace``. Remote URLs, path
    escapes, and non-image extensions return None.
    """
    dest = parse_markdown_link_dest(raw_dest)
    if not dest or _REMOTE_DEST_RE.match(dest):
        return None
    try:
        normalized = sanitize_harness_path(dest)
        if normalized != "/workspace" and not normalized.startswith("/workspace/"):
            return None
    except ValueError:
        return None
    lower = normalized.lower()
    if not any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return None
    return normalized


def workspace_media_path(path: str) -> str | None:
    """Return a safe workspace media path, rejecting traversal and external paths."""
    if not path.startswith("/workspace/") or any(
        part in {".", ".."} for part in path.split("/")
    ):
        return None
    try:
        normalized = sanitize_harness_path(path)
    except ValueError:
        return None
    return (
        normalized
        if normalized == path and normalized.startswith("/workspace/")
        else None
    )


def _code_mask(text: str) -> list[bool]:
    """Mark fenced and inline code so code examples never trigger file reads."""
    mask = [False] * len(text)
    fence: tuple[str, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            char, length = fence
            closing = re.match(
                rf" {{0,3}}{re.escape(char)}{{{length},}}\s*$", line.rstrip("\r\n")
            )
            if closing:
                fence = None
            mask[offset : offset + len(line)] = [True] * len(line)
        elif marker:
            run = marker.group(1)
            fence = (run[0], len(run))
            mask[offset : offset + len(line)] = [True] * len(line)
        offset += len(line)

    ticks = re.compile(r"(?<!`)`+(?!`)")
    cursor = 0
    while match := ticks.search(text, cursor):
        if mask[match.start()]:
            cursor = match.end()
            continue
        delimiter = "`" * (match.end() - match.start())
        close = re.compile(rf"(?<!`){re.escape(delimiter)}(?!`)").search(
            text, match.end()
        )
        if close is None or mask[close.start()]:
            cursor = match.end()
            continue
        mask[match.start() : close.end()] = [True] * (close.end() - match.start())
        cursor = close.end()
    return mask


def media_references(text: str) -> list[MediaReference]:
    """Find supported, safe media references outside code using shared spans."""
    masked = _code_mask(text)
    found: list[MediaReference] = []
    for match in MARKDOWN_IMAGE_RE.finditer(text):
        if any(masked[match.start() : match.end()]):
            continue
        raw_dest = match.group(2)
        dest = parse_markdown_link_dest(raw_dest)
        path = resolve_workspace_image_path(raw_dest)
        if (
            path is None
            or ".." in dest.split("/")
            or not path.lower().endswith(_IMAGE_EXTENSIONS)
        ):
            continue
        found.append(MediaReference(match.start(), match.end(), path, "image", True))

    for match in _FILE_REFERENCE.finditer(text):
        raw_path = match.group(1)
        path = raw_path.rstrip("),;!?]")
        end = match.start(1) + len(path)
        if any(masked[match.start() : end]):
            continue
        suffix = text[end:]
        if not path or (suffix and not re.match(r"[\s),;!?\]]", suffix)):
            continue
        if any(masked[match.start() : end]):
            continue
        safe_path = workspace_media_path(path)
        if safe_path is None or not safe_path.lower().endswith(_MEDIA_EXTENSIONS):
            continue
        kind = "document" if safe_path.lower().endswith(".pdf") else "image"
        found.append(MediaReference(match.start(), end, safe_path, kind, False))

    # Markdown image destinations and visible @file text are mutually exclusive
    # spans. Prefer the larger Markdown construct if unusual text overlaps them.
    found.sort(key=lambda item: (item.start, not item.markdown, -item.end))
    ordered: list[MediaReference] = []
    end = -1
    for reference in found:
        if reference.start < end:
            continue
        ordered.append(reference)
        end = reference.end
    return ordered
