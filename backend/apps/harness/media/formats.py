"""Detect attachment formats and validate canonical text/image/PDF blocks."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from ..access.file_types import guess_mime_type
from .limits import MAX_CLAUDE_DOCUMENT_BYTES, MAX_CLAUDE_IMAGE_BASE64_CHARS

SUPPORTED_IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}
SUPPORTED_PDF_MIME = "application/pdf"
DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$")


def _starts_with(content: bytes, prefix: tuple[int, ...]) -> bool:
    """Return True when *content* starts with the given byte prefix."""
    if len(content) < len(prefix):
        return False
    return all(byte == value for byte, value in zip(content, prefix))


def _sniff_image_mime(sample: bytes) -> str | None:
    """Detect an image MIME from magic bytes (OpenCode ``imageMime``).

    PNG/JPEG/GIF/WEBP are recognized from the first bytes, independent
    of the file extension. Returns None when no magic matches.
    """
    if _starts_with(sample, (0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)):
        return "image/png"
    if _starts_with(sample, (0xFF, 0xD8, 0xFF)):
        return "image/jpeg"
    if _starts_with(sample, (0x47, 0x49, 0x46, 0x38)):
        return "image/gif"
    if _starts_with(sample, (0x52, 0x49, 0x46, 0x46)) and _starts_with(
        sample[8:], (0x57, 0x45, 0x42, 0x50)
    ):
        return "image/webp"
    return None


def sniff_attachment_mime(sample: bytes, path: str, stored_mime: str = "") -> str:
    """Return the attachment MIME for *sample* (OpenCode parity).

    Magic bytes win over the stored/extension MIME so a JPEG saved as
    ``.bin`` is still detected as ``image/jpeg``. Falls back to
    *stored_mime* (runner ``file --mime-type``) and finally to the
    extension guess.
    """
    sniffed = _sniff_image_mime(sample)
    if sniffed is not None:
        return sniffed
    if _starts_with(sample, (0x25, 0x50, 0x44, 0x46)):
        return SUPPORTED_PDF_MIME
    if stored_mime and stored_mime != "application/octet-stream":
        return stored_mime
    return guess_mime_type(path)


def is_pdf_attachment(mime: str) -> bool:
    """Return True when *mime* denotes a PDF attachment."""
    return mime == SUPPORTED_PDF_MIME


def image_block(mime: str, encoded: str) -> dict[str, Any]:
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


def document_block(mime: str, encoded: str, filename: str = "") -> dict[str, Any]:
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
    match = DATA_URL_RE.fullmatch(url)
    if match is None:
        raise ValueError("Claude multimodal input must use a valid base64 data URL")
    mime, encoded = match.groups()
    if mime.lower() == "application/pdf":
        return document_block(mime, encoded, filename)
    return image_block(mime, encoded)


def part_blocks(part: Any) -> list[dict[str, Any]]:
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
    return [block for part in content for block in part_blocks(part)]
