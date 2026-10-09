"""Bounded conversion of OpenCuria MCP media results to Claude MCP content."""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import PurePosixPath
from typing import Any

from ...tools.files import MAX_MEDIA_INGEST_BYTES, SUPPORTED_IMAGE_MIMES
from .context import MAX_CLAUDE_IMAGE_BASE64_CHARS
from .materializer import stage_pdf_attachments

MAX_MCP_ATTACHMENT_COUNT = 8
MAX_MCP_ATTACHMENT_TOTAL_BYTES = 24 * 1024 * 1024
_DATA_URL = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$")


def _decode_attachment(attachment: dict[str, Any]) -> tuple[str, bytes, str] | None:
    """Strictly validate one supported image/PDF data-URL attachment."""
    mime = str(attachment.get("mime") or "").strip().lower()
    url = attachment.get("url")
    if not isinstance(url, str):
        return None
    match = _DATA_URL.fullmatch(url)
    if match is None:
        return None
    data_mime, encoded = match.groups()
    if data_mime.lower() != mime:
        return None
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in SUPPORTED_IMAGE_MIMES and mime != "application/pdf":
        return None
    raw_limit = (
        MAX_MEDIA_INGEST_BYTES
        if mime == "application/pdf"
        else MAX_CLAUDE_IMAGE_BASE64_CHARS * 3 // 4
    )
    if len(encoded) > raw_limit * 4 // 3 + 4:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not payload or len(payload) > raw_limit:
        return None
    if mime == "application/pdf" and not payload.startswith(b"%PDF-"):
        return None
    return mime, payload, encoded


def sanitize_mcp_attachments(
    attachments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Keep only bounded image/PDF attachments before event persistence."""
    safe: list[dict[str, Any]] = []
    total = 0
    for attachment in (attachments or [])[:MAX_MCP_ATTACHMENT_COUNT]:
        if not isinstance(attachment, dict):
            continue
        decoded = _decode_attachment(attachment)
        if decoded is None:
            continue
        mime, payload, encoded = decoded
        total += len(payload)
        if total > MAX_MCP_ATTACHMENT_TOTAL_BYTES:
            raise ValueError("MCP attachments exceed Claude's aggregate media limit")
        filename = str(attachment.get("filename") or "")
        filename = PurePosixPath(filename.replace("\\", "/")).name[:255]
        safe.append({
            "type": "file",
            "mime": mime,
            "url": f"data:{mime};base64,{encoded}",
            "filename": filename,
        })
    return safe


async def mcp_attachment_content(
    attachments: list[dict[str, Any]] | None,
    *,
    accessor: Any,
    managed_root: str,
    ensure_directory: Any,
) -> list[dict[str, Any]]:
    """Return SDK-supported image blocks and private paths for PDF attachments."""
    safe_attachments = sanitize_mcp_attachments(attachments)
    result: list[dict[str, Any]] = []
    staged: list[dict[str, Any]] = []
    for attachment in safe_attachments:
        decoded = _decode_attachment(attachment)
        if decoded is None:
            continue
        mime, _payload, encoded = decoded
        if mime == "application/pdf":
            staged.append(attachment)
        else:
            result.append({"type": "image", "data": encoded, "mimeType": mime})

    if staged:
        paths = await stage_pdf_attachments(
            accessor=accessor,
            root=managed_root,
            attachments=staged,
            ensure_directory=ensure_directory,
        )
        result.extend(
            {
                "type": "text",
                "text": (
                    "A PDF attachment was staged in the run-private workspace at "
                    f"{path}. Use the native Read tool to inspect it."
                ),
            }
            for path in paths
        )
    return result


__all__ = [
    "MAX_MCP_ATTACHMENT_COUNT",
    "MAX_MCP_ATTACHMENT_TOTAL_BYTES",
    "mcp_attachment_content",
    "sanitize_mcp_attachments",
]
