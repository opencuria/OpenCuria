"""Tool media lives in the workspace; storage keeps only path references."""

from __future__ import annotations

import base64
import hashlib

from apps.harness.media.attachments import (
    build_tool_message_content,
    stored_tool_attachments,
)
from apps.harness.media.tool_media import (
    TOOL_MEDIA_DIR,
    ToolMediaStore,
    is_tool_media_reference,
    tool_media_path,
)
from apps.harness.tests.conftest import FakeAccessor

PNG = b"\x89PNG\r\n\x1a\nfake"
PDF = b"%PDF-1.7 fake"


def _attachment(mime: str, payload: bytes, filename: str = "") -> dict:
    encoded = base64.b64encode(payload).decode()
    return {
        "type": "file",
        "mime": mime,
        "url": f"data:{mime};base64,{encoded}",
        "filename": filename,
    }


async def test_externalize_writes_content_addressed_files_once() -> None:
    """Identical bytes map to one workspace file; references carry no bytes."""
    accessor = FakeAccessor()
    store = ToolMediaStore(lambda _workspace_id: accessor)

    references = await store.externalize(
        "ws-1",
        [_attachment("image/png", PNG, "cat.png"), _attachment("application/pdf", PDF)],
    )

    png_path = f"{TOOL_MEDIA_DIR}/{hashlib.sha256(PNG).hexdigest()}.png"
    pdf_path = f"{TOOL_MEDIA_DIR}/{hashlib.sha256(PDF).hexdigest()}.pdf"
    assert references == [
        {"type": "file", "mime": "image/png", "path": png_path, "filename": "cat.png"},
        {"type": "file", "mime": "application/pdf", "path": pdf_path, "filename": ""},
    ]
    assert accessor.written == {png_path: PNG, pdf_path: PDF}
    again = await store.externalize("ws-1", [_attachment("image/png", PNG)])
    assert again[0]["path"] == png_path


async def test_externalize_rejects_unsupported_or_mismatched_media() -> None:
    """Only supported, self-consistent data URLs are written to the workspace."""
    accessor = FakeAccessor()
    store = ToolMediaStore(lambda _workspace_id: accessor)
    mismatched = {**_attachment("image/png", PNG), "mime": "image/jpeg"}

    references = await store.externalize(
        "ws-1",
        [mismatched, _attachment("image/svg+xml", b"<svg/>"), {"url": "nope"}],
    )

    assert references == []
    assert accessor.written == {}


async def test_externalize_drops_media_when_the_workspace_is_unavailable() -> None:
    """Write failures and a missing accessor never fall back to stored bytes."""
    failing = ToolMediaStore(lambda _workspace_id: FakeAccessor(error=OSError()))
    unwired = ToolMediaStore(None)

    assert await failing.externalize("ws-1", [_attachment("image/png", PNG)]) == []
    assert await unwired.externalize("ws-1", [_attachment("image/png", PNG)]) == []


def test_references_must_point_into_the_tool_media_directory() -> None:
    """Forged references to other workspace files are not accepted as media."""
    path = tool_media_path("image/png", PNG)
    valid = {"type": "file", "mime": "image/png", "path": path}
    forged = [
        {**valid, "path": "/workspace/secret.png"},
        {**valid, "path": f"{TOOL_MEDIA_DIR}/../../etc/passwd.png"},
        {**valid, "mime": "application/pdf"},
    ]

    assert is_tool_media_reference(valid)
    assert not any(is_tool_media_reference(item) for item in forged)
    assert stored_tool_attachments([*forged, valid]) == [{**valid, "filename": ""}]


def test_stored_attachments_keep_legacy_data_urls_for_replay() -> None:
    """Parts written before references keep replaying their inline image."""
    legacy = _attachment("image/png", PNG, "old.png")

    content = build_tool_message_content("seen", stored_tool_attachments([legacy]))

    assert content[1] == {"type": "image_url", "image_url": {"url": legacy["url"]}}
