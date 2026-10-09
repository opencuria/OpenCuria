"""Bounded, private staging for non-image MCP media attachments."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

from ...tools.files import MAX_MEDIA_INGEST_BYTES

MAX_STAGED_ATTACHMENT_COUNT = 8
MAX_STAGED_ATTACHMENT_TOTAL_BYTES = 24 * 1024 * 1024
_DATA_URL = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$")


def decode_pdf_attachment(attachment: dict[str, Any]) -> bytes | None:
    """Decode one strictly validated PDF data-URL attachment."""
    if str(attachment.get("mime") or "").strip().lower() != "application/pdf":
        return None
    url = attachment.get("url")
    if not isinstance(url, str):
        return None
    match = _DATA_URL.fullmatch(url)
    if match is None or match.group(1).lower() != "application/pdf":
        return None
    encoded = match.group(2)
    if len(encoded) > MAX_MEDIA_INGEST_BYTES * 4 // 3 + 4:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not payload.startswith(b"%PDF-") or len(payload) > MAX_MEDIA_INGEST_BYTES:
        return None
    return payload


async def stage_pdf_attachments(
    *,
    accessor: Any,
    root: str,
    attachments: list[dict[str, Any]],
    ensure_directory: Callable[[str], Awaitable[None]],
) -> list[str]:
    """Write validated PDFs below one private run root using opaque names."""
    paths: list[str] = []
    total = 0
    directory = f"{root}/config/attachments"
    for attachment in attachments[:MAX_STAGED_ATTACHMENT_COUNT]:
        payload = decode_pdf_attachment(attachment)
        if payload is None:
            continue
        total += len(payload)
        if total > MAX_STAGED_ATTACHMENT_TOTAL_BYTES:
            raise ValueError("MCP attachments exceed Claude's aggregate media limit")
        if not paths:
            await ensure_directory(directory)
        digest = hashlib.sha256(payload).hexdigest()
        path = f"{directory}/{digest}.pdf"
        await accessor.write_file(path, payload, mode=0o600)
        paths.append(path)
    return paths


__all__ = [
    "MAX_STAGED_ATTACHMENT_COUNT",
    "MAX_STAGED_ATTACHMENT_TOTAL_BYTES",
    "decode_pdf_attachment",
    "stage_pdf_attachments",
]
