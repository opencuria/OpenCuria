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
