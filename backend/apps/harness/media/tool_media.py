"""Keep tool media bytes in the workspace and only path references at rest.

Tool images and PDFs are written once, content-addressed, below
``TOOL_MEDIA_DIR``. Persisted parts, engine transcripts and checkpoints keep a
path reference or a short notice instead of base64, so conversation storage
stays small and restores stay within their limits.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from ..access.base import HARNESS_WORKSPACE_ROOT, WorkspaceAccessor
from ..image_processing import image_path_notice
from .formats import DATA_URL_RE

log = structlog.get_logger(__name__)

#: Workspace directory holding content-addressed tool images and PDFs.
TOOL_MEDIA_DIR = f"{HARNESS_WORKSPACE_ROOT}/.opencuria/tool-media"

#: Replaces inline media in stored engine transcripts and checkpoints.
OMITTED_MEDIA_NOTICE = (
    "[Media omitted from stored history. Read the file again or capture a new "
    "screenshot if you need to see it.]"
)

_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
}
_PATH_RE = re.compile(
    rf"^{re.escape(TOOL_MEDIA_DIR)}/[0-9a-f]{{64}}\.({'|'.join(_EXTENSIONS.values())})$"
)

AccessorFactory = Callable[[str], "Awaitable[WorkspaceAccessor] | WorkspaceAccessor"]


def tool_media_path(mime: str, content: bytes) -> str:
    """Return the content-addressed workspace path for one media payload."""
    return f"{TOOL_MEDIA_DIR}/{hashlib.sha256(content).hexdigest()}.{_EXTENSIONS[mime]}"


def is_tool_media_reference(attachment: object) -> bool:
    """Return True for a ``{type, mime, path, filename}`` tool media reference."""
    if not isinstance(attachment, dict) or attachment.get("type") != "file":
        return False
    path, mime = attachment.get("path"), attachment.get("mime")
    match = _PATH_RE.fullmatch(path) if isinstance(path, str) else None
    return match is not None and _EXTENSIONS.get(str(mime)) == match.group(1)


def tool_media_notice(reference: dict[str, Any]) -> str:
    """Tell the model where an earlier tool image or PDF can be read again."""
    path = str(reference.get("path") or "")
    if reference.get("mime") == "application/pdf":
        return f"\n[PDF not loaded: earlier tool result. Original file: {path}.]\n"
    return image_path_notice(path, "earlier tool result; read it again if needed")


def _decode(attachment: dict[str, Any]) -> tuple[str, bytes] | None:
    """Return the MIME and bytes of a supported data-URL attachment."""
    url = attachment.get("url")
    match = DATA_URL_RE.fullmatch(url) if isinstance(url, str) else None
    mime = str(attachment.get("mime") or "").strip().lower()
    if match is None or match.group(1).lower() != mime or mime not in _EXTENSIONS:
        return None
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        return None
    return (mime, payload) if payload else None


class ToolMediaStore:
    """Write tool media into its workspace and return path references."""

    def __init__(self, accessor_factory: AccessorFactory | None) -> None:
        """Bind the workspace accessor factory used for media writes."""
        self._accessor_factory = accessor_factory

    async def externalize(
        self, workspace_id: str, attachments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace data-URL attachments with references to workspace files.

        Existing references pass through. An attachment that cannot be written
        is dropped (and logged) so no media bytes are ever persisted.
        """
        references: list[dict[str, Any]] = []
        accessor: WorkspaceAccessor | None = None
        for attachment in attachments:
            if is_tool_media_reference(attachment):
                references.append(dict(attachment))
                continue
            decoded = _decode(attachment)
            if decoded is None:
                continue
            mime, payload = decoded
            path = tool_media_path(mime, payload)
            try:
                if accessor is None:
                    accessor = await self._accessor(workspace_id)
                await accessor.write_file(path, payload)
            except Exception as exc:  # noqa: BLE001 - never fail the tool part
                log.warning(
                    "tool_media_not_persisted",
                    workspace_id=workspace_id,
                    mime=mime,
                    error=type(exc).__name__,
                )
                continue
            references.append(
                {
                    "type": "file",
                    "mime": mime,
                    "path": path,
                    "filename": str(attachment.get("filename") or ""),
                }
            )
        return references

    async def _accessor(self, workspace_id: str) -> WorkspaceAccessor:
        """Resolve the workspace accessor from a sync or async factory."""
        if self._accessor_factory is None:
            raise RuntimeError("No workspace accessor configured")
        accessor = self._accessor_factory(workspace_id)
        if asyncio.iscoroutine(accessor):
            accessor = await accessor
        return accessor
