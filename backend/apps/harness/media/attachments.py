"""Map tool attachments to provider content and bounded persisted metadata."""

from __future__ import annotations

from typing import Any

import structlog

from .limits import TOOL_ATTACHMENT_MAX_CHARS, TOOL_ATTACHMENT_MAX_COUNT
from .tool_media import is_tool_media_reference, tool_media_notice

log = structlog.get_logger(__name__)


def _attachment_mime(attachment: object) -> str:
    """Return the MIME type of a file attachment dict, or ""."""
    if not isinstance(attachment, dict):
        return ""
    mime = attachment.get("mime", "")
    return str(mime or "").strip().lower()


def _attachment_url(attachment: object) -> str:
    """Return the data URL of a file attachment dict, or ""."""
    if not isinstance(attachment, dict):
        return ""
    url = attachment.get("url", "")
    return str(url or "") if isinstance(url, str) else ""


def _attachment_filename(attachment: object) -> str:
    """Return the filename of a file attachment dict, or ""."""
    if not isinstance(attachment, dict):
        return ""
    name = attachment.get("filename", "")
    return str(name or "") if isinstance(name, str) else ""


def is_image_attachment(attachment: object) -> bool:
    """Return True when *attachment* is an image file attachment."""
    return _attachment_mime(attachment).startswith("image/")


def is_pdf_attachment(attachment: object) -> bool:
    """Return True when *attachment* is a PDF file attachment."""
    return _attachment_mime(attachment) == "application/pdf"


def tool_message_image_parts(
    attachments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Map image attachments to OpenAI-style ``image_url`` parts.

    Only ``image/*`` attachments are mapped. PDF attachments are
    handled by :func:`tool_message_file_parts` below (they cannot be
    sent as ``image_url`` without confusing providers).
    """
    parts: list[dict[str, Any]] = []
    for attachment in attachments or []:
        if not is_image_attachment(attachment):
            continue
        url = _attachment_url(attachment)
        if not url or not url.startswith("data:"):
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def tool_message_file_parts(
    attachments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Map PDF attachments to canonical ``file`` parts.

    Canonical shape is ``{"type": "file", "mime", "url", "filename"}``.
    Only ``application/pdf`` data-URL attachments are mapped; images
    and unknown MIMEs are skipped defensively.
    """
    parts: list[dict[str, Any]] = []
    for attachment in attachments or []:
        if not is_pdf_attachment(attachment):
            continue
        url = _attachment_url(attachment)
        if not url or not url.startswith("data:"):
            continue
        parts.append(
            {
                "type": "file",
                "mime": "application/pdf",
                "url": url,
                "filename": _attachment_filename(attachment),
            }
        )
    return parts


def build_tool_message_content(
    output: str,
    attachments: list[dict[str, Any]] | None,
    filename: str = "",
) -> str | list[dict[str, Any]]:
    """Build tool-role content from text plus image/PDF attachments.

    Returns the plain *output* string when no attachment maps to a
    multimodal part, otherwise a list of ``[{"type": "text", ...},
    {"type": "image_url", ...}, {"type": "file", ...}]`` so the LLM
    receives images and PDFs (OpenCode parity: PDFs travel as file
    parts, not text-only). Workspace media references from earlier
    turns become path notices the model can read again. *filename* is
    kept for backwards compatibility and currently unused (PDF
    attachments carry their own ``filename``). Callers truncating long
    tool output must only clip the text part, never base64.
    """
    text = output or ""
    text += "".join(
        tool_media_notice(attachment)
        for attachment in attachments or []
        if is_tool_media_reference(attachment)
    )
    image_parts = tool_message_image_parts(attachments)
    file_parts = tool_message_file_parts(attachments)
    if not image_parts and not file_parts:
        return text
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(image_parts)
    content.extend(file_parts)
    return content


def select_persisted_tool_attachments(
    attachments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Select the data-URL attachments a tool part keeps as workspace media.

    Keeps at most ``TOOL_ATTACHMENT_MAX_COUNT`` entries (images and PDFs
    alike) and drops entries without a data URL or above
    ``TOOL_ATTACHMENT_MAX_CHARS``. ``ToolMediaStore`` then replaces the
    selected bytes with workspace path references before persistence.
    Returns defensive copies with only ``type``/``mime``/``url`` plus
    ``filename`` (non-string filenames fall back to ``""``) keys.
    """
    kept: list[dict[str, Any]] = []
    for attachment in attachments or []:
        if len(kept) >= TOOL_ATTACHMENT_MAX_COUNT:
            break
        if not isinstance(attachment, dict):
            continue
        url = _attachment_url(attachment)
        mime = _attachment_mime(attachment)
        if not url or not url.startswith("data:"):
            continue
        if len(url) > TOOL_ATTACHMENT_MAX_CHARS:
            log.warning(
                "tool_attachment_not_persisted",
                mime=mime,
                chars=len(url),
            )
            continue
        kept.append(
            {
                "type": "file",
                "mime": mime,
                "url": url,
                "filename": _attachment_filename(attachment),
            }
        )
    return kept


def stored_tool_attachments(
    attachments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return persisted part attachments: workspace references or legacy data URLs.

    Keeps at most ``TOOL_ATTACHMENT_MAX_COUNT`` entries. References must point
    at a content-addressed tool media file; legacy entries are re-sanitized.
    """
    kept: list[dict[str, Any]] = []
    for attachment in attachments or []:
        if len(kept) >= TOOL_ATTACHMENT_MAX_COUNT:
            break
        if is_tool_media_reference(attachment):
            kept.append(
                {
                    "type": "file",
                    "mime": attachment["mime"],
                    "path": attachment["path"],
                    "filename": _attachment_filename(attachment),
                }
            )
        else:
            kept.extend(select_persisted_tool_attachments([attachment]))
    return kept
