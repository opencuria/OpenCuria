"""Keep inline media bytes out of stored and restored Claude transcripts."""

from __future__ import annotations

from typing import Any

from ...media.tool_media import OMITTED_MEDIA_NOTICE


def _is_media_block(value: dict[str, Any]) -> bool:
    """Match API image/document blocks and MCP image items carrying base64."""
    source = value.get("source")
    if value.get("type") in {"image", "document"} and isinstance(source, dict):
        return source.get("type") == "base64" and isinstance(source.get("data"), str)
    return value.get("type") == "image" and isinstance(value.get("data"), str)


def strip_transcript_media(entry: Any) -> Any:
    """Return a copy of *entry* without inline base64 media.

    Model-visible media blocks become one short text notice. Native Read
    results keep their metadata with ``file.base64`` emptied, the same form
    Claude Code uses when it slims transcript rows itself.
    """
    if isinstance(entry, list):
        return [strip_transcript_media(item) for item in entry]
    if not isinstance(entry, dict):
        return entry
    if _is_media_block(entry):
        return {"type": "text", "text": OMITTED_MEDIA_NOTICE}
    stripped = {key: strip_transcript_media(value) for key, value in entry.items()}
    file = stripped.get("file")
    if (
        stripped.get("type") in {"image", "pdf"}
        and isinstance(file, dict)
        and file.get("base64")
    ):
        stripped["file"] = {**file, "base64": ""}
    return stripped
