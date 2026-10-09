"""Bounded conversion of OpenCuria prompt images to Claude API blocks."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from ...access.base import WorkspaceAccessor
from ...images import hydrate_user_messages
from ...providers.base import LLMMessage
from ...tools.files import MAX_MEDIA_INGEST_BYTES, SUPPORTED_IMAGE_MIMES

MAX_CLAUDE_IMAGE_BASE64_CHARS = 5_000_000
MAX_CLAUDE_DOCUMENT_BYTES = MAX_MEDIA_INGEST_BYTES
MAX_USER_IMAGE_BLOCKS = 16
MAX_USER_CONTENT_BYTES = 12 * 1024 * 1024
_DATA_URL = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$")


class _BoundedImageAccessor:
    """Proxy image reads to bound bytes before shared workspace hydration."""

    def __init__(self, accessor: WorkspaceAccessor) -> None:
        self._accessor = accessor
        self._total_bytes = 0
        self._image_count = 0
        self._file_bytes: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._accessor, name)

    async def read_file(self, path: str, max_size: int | None = None) -> Any:
        """Read an image within the per-image and aggregate request bounds."""
        already_read = self._file_bytes.get(path)
        if already_read is not None:
            stored = await self._accessor.read_file(path, max_size=max_size)
            if len(stored.content) != already_read:
                self._total_bytes += len(stored.content) - already_read
                self._file_bytes[path] = len(stored.content)
            return stored
        if self._image_count >= MAX_USER_IMAGE_BLOCKS:
            raise OSError("Claude workspace image count limit exceeded")
        max_raw = MAX_CLAUDE_IMAGE_BASE64_CHARS * 3 // 4
        limit = max_raw + 1 if max_size is None else min(max_size, max_raw + 1)
        stored = await self._accessor.read_file(path, max_size=limit)
        self._image_count += 1
        if stored.truncated or len(stored.content) > max_raw:
            raise OSError("Workspace image exceeds Claude's per-image input limit")
        self._total_bytes += len(stored.content)
        self._file_bytes[path] = len(stored.content)
        if self._total_bytes > max_raw * 2:
            raise OSError("Claude aggregate image limit exceeded")
        return stored


def _image_block(mime: str, encoded: str) -> dict[str, Any]:
    """Return an Anthropic image block from strictly validated base64."""
    normalized = "image/jpeg" if mime.lower() == "image/jpg" else mime.lower()
    if normalized not in SUPPORTED_IMAGE_MIMES:
        raise ValueError(f"Unsupported Claude image media type: {mime}")
    if not encoded or len(encoded) > MAX_CLAUDE_IMAGE_BASE64_CHARS:
        raise ValueError("Claude image exceeds the per-image input limit")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Claude image contains invalid base64 data") from None
    if not payload:
        raise ValueError("Claude image is empty")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": normalized, "data": encoded},
    }


def _document_block(mime: str, encoded: str, filename: str = "") -> dict[str, Any]:
    """Return a Claude PDF document block from strictly validated base64."""
    if mime.lower() != "application/pdf" or not encoded:
        raise ValueError("Unsupported Claude document media type")
    if len(encoded) > MAX_CLAUDE_DOCUMENT_BYTES * 4 // 3 + 4:
        raise ValueError("Claude PDF exceeds the input limit")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Claude PDF contains invalid base64 data") from None
    if not payload.startswith(b"%PDF-") or len(payload) > MAX_CLAUDE_DOCUMENT_BYTES:
        raise ValueError("Claude PDF is invalid or exceeds the input limit")
    block: dict[str, Any] = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
    }
    if filename:
        block["title"] = filename[:255]
    return block


def data_url_block(url: str, *, filename: str = "") -> dict[str, Any]:
    """Parse a data URL without remote fetches or permissive MIME coercion."""
    match = _DATA_URL.fullmatch(url)
    if match is None:
        raise ValueError("Claude multimodal input must use a valid base64 data URL")
    mime, encoded = match.groups()
    if mime.lower() == "application/pdf":
        return _document_block(mime, encoded, filename)
    return _image_block(mime, encoded)


def _part_blocks(part: Any) -> list[dict[str, Any]]:
    """Convert canonical harness text/image/file parts to Anthropic blocks."""
    if not isinstance(part, dict):
        raise ValueError("Claude message content part must be an object")
    kind = str(part.get("type") or "")
    if kind == "text":
        text = part.get("text")
        return (
            [{"type": "text", "text": text}] if isinstance(text, str) and text else []
        )
    if kind == "image_url":
        value = part.get("image_url")
        url = value.get("url") if isinstance(value, dict) else value
        if not isinstance(url, str):
            raise ValueError("Claude image_url part has no data URL")
        block = data_url_block(url)
        if block["type"] != "image":
            raise ValueError("Claude image_url part must contain an image")
        return [block]
    if kind == "file":
        url = part.get("url")
        if not isinstance(url, str):
            raise ValueError("Claude file part has no data URL")
        block = data_url_block(url, filename=str(part.get("filename") or ""))
        if block["type"] != "document":
            raise ValueError("Claude file part must contain a PDF")
        mime = str(part.get("mime") or "").lower()
        if mime and mime != "application/pdf":
            raise ValueError("Claude file MIME does not match its PDF data URL")
        return [block]
    raise ValueError(f"Unsupported Claude message part: {kind or 'unknown'}")


def content_blocks(content: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert canonical message content without interpreting arbitrary URLs."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ValueError("Claude message content has an unsupported shape")
    return [block for part in content for block in _part_blocks(part)]


def checked_content(blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Enforce request-wide content/media bounds and simplify text-only input."""
    text_bytes = 0
    media_chars = 0
    image_bytes = 0
    document_bytes = 0
    image_count = 0
    for block in blocks:
        if block.get("type") == "text":
            text_bytes += len(str(block.get("text") or "").encode("utf-8"))
            continue
        source = block.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("data"), str):
            raise ValueError("Claude media block has no valid base64 source")
        media_chars += len(source["data"])
        if block.get("type") == "image":
            image_count += 1
            image_bytes += (len(source["data"]) * 3) // 4
        else:
            document_bytes += (len(source["data"]) * 3) // 4
    if image_count > MAX_USER_IMAGE_BLOCKS:
        raise ValueError("Claude request has too many image blocks")
    if image_bytes > MAX_CLAUDE_IMAGE_BASE64_CHARS * 3 // 2:
        raise ValueError("Claude aggregate image limit exceeded")
    if document_bytes > MAX_CLAUDE_DOCUMENT_BYTES * 3 // 4:
        raise ValueError("Claude aggregate PDF limit exceeded")
    if text_bytes + media_chars > MAX_USER_CONTENT_BYTES:
        raise ValueError("Claude request content exceeds the input limit")
    if not any(block.get("type") in {"image", "document"} for block in blocks):
        return "".join(str(block.get("text") or "") for block in blocks)
    return blocks


async def _markdown_blocks(
    text: str, accessor: WorkspaceAccessor | _BoundedImageAccessor
) -> list[dict[str, Any]]:
    """Hydrate only bounded, local workspace Markdown images."""
    bounded = (
        accessor
        if isinstance(accessor, _BoundedImageAccessor)
        else _BoundedImageAccessor(accessor)
    )
    hydrated = await hydrate_user_messages(
        [LLMMessage(role="user", content=text)], bounded
    )
    return content_blocks(hydrated[0].content)


async def _history_blocks(
    message: LLMMessage, accessor: WorkspaceAccessor | _BoundedImageAccessor
) -> list[dict[str, Any]]:
    """Preserve text and multimodal history parts in original order."""
    if message.role == "user" and isinstance(message.content, str):
        return await _markdown_blocks(message.content, accessor)
    if isinstance(message.content, list):
        output: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    output.extend(await _markdown_blocks(text, accessor))
                    continue
            output.extend(_part_blocks(part))
        return output
    return content_blocks(message.content)


async def build_user_content(
    prompt: str,
    *,
    accessor: WorkspaceAccessor,
    history: list[LLMMessage] | None = None,
    include_history: bool = True,
) -> str | list[dict[str, Any]]:
    """Hydrate current prompt and multimodal history into Claude API blocks."""
    bounded_accessor = _BoundedImageAccessor(accessor)
    blocks: list[dict[str, Any]] = []
    if include_history and history:
        blocks.append({"type": "text", "text": "Prior conversation context:"})
        for message in history:
            role = str(message.role or "user").title()
            blocks.append({"type": "text", "text": f"\n\n{role}: "})
            blocks.extend(await _history_blocks(message, bounded_accessor))
            if message.role == "tool" and message.tool_call_id:
                blocks.append(
                    {
                        "type": "text",
                        "text": f"\nTool result id: {message.tool_call_id}",
                    }
                )
        blocks.append({"type": "text", "text": "\n\nCurrent user request: "})
    blocks.extend(await _markdown_blocks(prompt.strip(), bounded_accessor))
    return checked_content(blocks)


__all__ = [
    "MAX_CLAUDE_DOCUMENT_BYTES",
    "MAX_CLAUDE_IMAGE_BASE64_CHARS",
    "MAX_USER_CONTENT_BYTES",
    "build_user_content",
    "checked_content",
    "content_blocks",
    "data_url_block",
]
