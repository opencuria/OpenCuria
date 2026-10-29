"""Hydrate workspace image markdown into multimodal provider message parts."""

from __future__ import annotations

import re
from typing import Any

import structlog

from .access.base import WorkspaceAccessor, sanitize_harness_path
from .providers.base import LLMMessage

log = structlog.get_logger(__name__)

#: Any markdown image; workspace vs remote is decided after dest parsing.
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

#: Hydration is image-only (png/jpeg/gif/webp). Videos stay frontend-only.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_REMOTE_DEST_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)

IMAGE_TOKEN_ESTIMATE = 1000

#: Max tool attachments persisted into ``HarnessPart.meta`` (no DB column).
#: Keeps restarts bounded: the read tool emits at most one attachment per
#: call, so two covers parallel rounds without bloating the JSON meta.
TOOL_ATTACHMENT_MAX_COUNT = 2

#: Max base64 data-URL chars kept per persisted attachment (~6 MB binary).
#: Oversized attachments are dropped (text output survives); base64 can
#: never be truncated without corrupting the image.
TOOL_ATTACHMENT_MAX_CHARS = 8_000_000


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
    parts, not text-only). *filename* is kept for backwards
    compatibility and currently unused (PDF attachments carry their
    own ``filename``). Callers truncating long tool output must only
    clip the text part, never base64.
    """
    text = output or ""
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
    """Sanitize attachments for ``HarnessPart.meta`` persistence.

    Keeps at most ``TOOL_ATTACHMENT_MAX_COUNT`` entries (images and PDFs
    alike; PDFs stay available for file-part rehydration) and drops
    entries without a data URL or above ``TOOL_ATTACHMENT_MAX_CHARS``.
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


async def hydrate_workspace_images(
    content: str,
    accessor: WorkspaceAccessor,
) -> str | list[dict[str, Any]]:
    """Hydrate image mentions and Markdown using the policy shared by all engines."""
    messages = await hydrate_user_messages(
        [LLMMessage(role="user", content=content)], accessor
    )
    return messages[0].content


async def hydrate_user_messages(
    messages: list[LLMMessage],
    accessor: WorkspaceAccessor,
) -> list[LLMMessage]:
    """Hydrate user images with one count and byte budget for the conversation."""
    # Shared media also uses this module's Markdown parser and attachment helpers.
    from .engines.shared.media import hydrate_image_messages

    return await hydrate_image_messages(messages, accessor)
