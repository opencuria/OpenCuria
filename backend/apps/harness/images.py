"""Hydrate workspace image markdown into multimodal provider message parts."""

from __future__ import annotations

import base64
import re
from typing import Any

import structlog

from .access.base import WorkspaceAccessor, guess_mime_type
from .access.runner_accessor import RunnerAccessorError
from .providers.base import LLMMessage

log = structlog.get_logger(__name__)

WORKSPACE_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\((/workspace/[^)]+\.(?:png|jpg|jpeg|gif|webp))\)",
    re.IGNORECASE,
)

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


async def hydrate_workspace_images(
    content: str,
    accessor: WorkspaceAccessor,
) -> str | list[dict[str, Any]]:
    """Replace workspace image markdown with multimodal provider parts.

    Markdown like ``![label](/workspace/cat.png)`` is kept in the text part
    and supplemented with an ``image_url`` data-URL part. Missing files are
    skipped with a warning so a broken attachment does not abort the run.
    """
    if not content or not WORKSPACE_IMAGE_RE.search(content):
        return content

    parts: list[dict[str, Any]] = []
    last_end = 0
    for match in WORKSPACE_IMAGE_RE.finditer(content):
        text_before = content[last_end : match.start()]
        if text_before:
            parts.append({"type": "text", "text": text_before})
        image_path = match.group(2)
        image_part = await _image_part_for_path(image_path, accessor)
        if image_part is not None:
            parts.append(image_part)
        else:
            parts.append({"type": "text", "text": match.group(0)})
        last_end = match.end()

    trailing = content[last_end:]
    if trailing:
        parts.append({"type": "text", "text": trailing})

    if not parts:
        return content
    if not any(part.get("type") == "image_url" for part in parts):
        return content
    if len(parts) == 1 and parts[0].get("type") == "text":
        return str(parts[0].get("text", ""))
    return parts


async def hydrate_user_messages(
    messages: list[LLMMessage],
    accessor: WorkspaceAccessor,
) -> list[LLMMessage]:
    """Hydrate workspace images in user messages for provider requests."""
    hydrated: list[LLMMessage] = []
    for message in messages:
        if message.role == "user" and isinstance(message.content, str):
            content = await hydrate_workspace_images(message.content, accessor)
            hydrated.append(
                LLMMessage(
                    role=message.role,
                    content=content,
                    tool_calls=message.tool_calls,
                    tool_call_id=message.tool_call_id,
                )
            )
            continue
        hydrated.append(message)
    return hydrated


async def _image_part_for_path(
    path: str,
    accessor: WorkspaceAccessor,
) -> dict[str, Any] | None:
    """Read *path* and return an OpenAI-style image_url part, or None."""
    try:
        file_content = await accessor.read_file(path)
    except (RunnerAccessorError, ValueError, OSError) as exc:
        log.warning(
            "workspace_image_hydration_failed",
            path=path,
            error=str(exc),
        )
        return None
    mime = file_content.mime
    if not mime or mime == "application/octet-stream":
        mime = guess_mime_type(path)
    encoded = base64.b64encode(file_content.content).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }
