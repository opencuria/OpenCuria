"""Claude history and explicit user media-reference tests."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from apps.harness.access.base import FileContent
from apps.harness.engines.claude import context
from apps.harness.engines.claude.context import (
    MAX_CLAUDE_TOOL_ARGUMENT_BYTES,
    MAX_USER_CONTENT_BYTES,
    MediaAuthorizationError,
    MediaLimitError,
    build_user_content,
)
from apps.harness.providers.base import LLMMessage


class MediaAccessor:
    """Small accessor fake that records and bounds explicit reads."""

    workspace_id = "workspace-test"

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = files or {}
        self.reads: list[tuple[str, int | None]] = []
        self.no_follow_reads: list[tuple[str, int | None]] = []

    async def read_file_no_follow(
        self, path: str, max_size: int | None = None
    ) -> FileContent:
        """Record safe media reads and delegate the fake file payload."""
        self.no_follow_reads.append((path, max_size))
        return await self.read_file(path, max_size=max_size)

    async def read_file(self, path: str, max_size: int | None = None) -> FileContent:
        self.reads.append((path, max_size))
        if path not in self.files:
            raise FileNotFoundError(path)
        content = self.files[path]
        original_size = len(content)
        if max_size is not None:
            content = content[:max_size]
        return FileContent(
            content=content,
            size=original_size,
            truncated=len(content) < original_size,
            mime="application/octet-stream",
        )


def _media_blocks(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return []
    return [block for block in content if block.get("type") in {"image", "document"}]


def _text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if block.get("type") == "text"
    )


@pytest.mark.asyncio
async def test_current_and_history_user_file_refs_load_png_and_pdf_in_place() -> None:
    png = b"\x89PNG\r\n\x1a\nmedia"
    pdf = b"%PDF-1.7\nmedia"
    accessor = MediaAccessor({"/workspace/one.png": png, "/workspace/two.pdf": pdf})
    content = await build_user_content(
        "Before @file:/workspace/one.png after",
        accessor=accessor,
        history=[LLMMessage(role="user", content="Prior @file:/workspace/two.pdf now")],
    )

    assert isinstance(content, list)
    media = _media_blocks(content)
    assert [(block["type"], block["source"]["media_type"]) for block in media] == [
        ("document", "application/pdf"),
        ("image", "image/png"),
    ]
    assert base64.b64decode(media[0]["source"]["data"]) == pdf
    assert base64.b64decode(media[1]["source"]["data"]) == png
    assert "Prior @file:/workspace/two.pdf now" in _text(content)
    assert "Before @file:/workspace/one.png after" in _text(content)
    assert [path for path, _ in accessor.reads] == [
        "/workspace/two.pdf",
        "/workspace/one.png",
    ]
    assert all(max_size is not None for _, max_size in accessor.no_follow_reads)
    assert len(accessor.no_follow_reads) == len(accessor.reads)


@pytest.mark.asyncio
async def test_user_media_requires_optional_authorization_before_read() -> None:
    accessor = MediaAccessor({"/workspace/screenshot.png": b"\x89PNG\r\n\x1a\nscreen"})
    authorized: list[str] = []

    async def authorize(path: str) -> bool:
        authorized.append(path)
        return False

    with pytest.raises(MediaAuthorizationError, match="denied.*screenshot.png"):
        await build_user_content(
            "Inspect @file:/workspace/screenshot.png",
            accessor=accessor,
            authorize_read=authorize,
        )
    assert authorized == ["/workspace/screenshot.png"]
    assert accessor.reads == []


@pytest.mark.asyncio
async def test_user_media_rejects_traversal_and_nonmedia_refs_without_reads() -> None:
    accessor = MediaAccessor({"/workspace/photo.png": b"\x89PNG\r\n\x1a\nimage"})
    content = await build_user_content(
        "@file:/workspace/../workspace/photo.png @file:/workspace/photo.png.bak "
        "@file:/tmp/outside.png @file:/workspace/photo.png",
        accessor=accessor,
    )
    assert len(accessor.reads) == 1
    assert "@file:/workspace/../workspace/photo.png" in _text(content)
    assert "@file:/workspace/photo.png.bak" in _text(content)
    assert "@file:/tmp/outside.png" in _text(content)
    assert len(_media_blocks(content)) == 1


@pytest.mark.asyncio
async def test_dot_relative_markdown_image_hydrates_and_parent_traversal_does_not() -> None:
    png = b"\x89PNG\r\n\x1a\nimage"
    accessor = MediaAccessor({"/workspace/screenshots/cat.png": png})

    content = await build_user_content(
        "![cat](./screenshots/cat.png)", accessor=accessor
    )
    assert isinstance(content, list)
    image = next(block for block in _media_blocks(content) if block["type"] == "image")
    assert base64.b64decode(image["source"]["data"]) == png
    assert accessor.reads == [("/workspace/screenshots/cat.png", accessor.reads[0][1])]

    accessor.reads.clear()
    content = await build_user_content(
        "![outside](../screenshots/cat.png)", accessor=accessor
    )
    assert not _media_blocks(content)
    assert accessor.reads == []
    assert "![outside](../screenshots/cat.png)" == _text(content)


@pytest.mark.asyncio
async def test_repeated_media_is_read_once_and_count_limit_preflights() -> None:
    png = b"\x89PNG\r\n\x1a\nimage"
    accessor = MediaAccessor({"/workspace/a.png": png})
    authorized: list[str] = []

    async def authorize(path: str) -> bool:
        authorized.append(path)
        return True

    content = await build_user_content(
        " ".join("@file:/workspace/a.png" for _ in range(3)),
        accessor=accessor,
        authorize_read=authorize,
    )
    assert len(_media_blocks(content)) == 3
    assert len(accessor.reads) == 1
    assert authorized == ["/workspace/a.png"]

    accessor = MediaAccessor({"/workspace/a.png": png})
    with pytest.raises(MediaLimitError, match="too many image blocks"):
        await build_user_content(
            " ".join(
                "@file:/workspace/a.png"
                for _ in range(context.MAX_USER_IMAGE_BLOCKS + 1)
            ),
            accessor=accessor,
        )
    assert accessor.reads == []


@pytest.mark.asyncio
async def test_oversized_explicit_media_fails_without_returning_partial_payload() -> None:
    limit = context.MAX_CLAUDE_IMAGE_BASE64_CHARS * 3 // 4
    accessor = MediaAccessor(
        {"/workspace/too-large.png": b"\x89PNG\r\n\x1a\n" + b"x" * limit}
    )
    with pytest.raises(MediaLimitError, match="exceeds Claude's input limit"):
        await build_user_content("@file:/workspace/too-large.png", accessor=accessor)
    assert len(accessor.reads) == 1
    assert accessor.reads[0][1] == limit + 1


@pytest.mark.asyncio
async def test_inline_history_media_is_accounted_once_with_workspace_refs() -> None:
    png = b"\x89PNG\r\n\x1a\nimage"
    encoded = base64.b64encode(png).decode()
    accessor = MediaAccessor({"/workspace/a.png": png})
    history = [
        LLMMessage(
            role="user",
            content=[
                {"type": "text", "text": "prior"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ],
        )
    ]
    content = await build_user_content(
        "@file:/workspace/a.png", accessor=accessor, history=history
    )
    assert len(_media_blocks(content)) == 2
    assert len(accessor.reads) == 1

    # Inline canonical media alone remains valid at normal request sizes.
    assert len(
        _media_blocks(
            await build_user_content("current", accessor=accessor, history=history)
        )
    ) == 1


@pytest.mark.asyncio
async def test_media_aggregate_limit_includes_inline_history_before_any_read() -> None:
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()
    accessor = MediaAccessor({"/workspace/a.png": b"\x89PNG\r\n\x1a\nimage"})
    # Keep text within the request budget; the tiny PNG itself is well within the
    # request-wide byte cap. Multiple inline blocks below cross the max block cap
    # before any reference read, which proves preflight and mapping share counts.
    history = [
        LLMMessage(
            role="user",
            content=[
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
                for _ in range(context.MAX_USER_IMAGE_BLOCKS)
            ],
        )
    ]
    with pytest.raises(MediaLimitError, match="too many image blocks"):
        await build_user_content("@file:/workspace/a.png", accessor=accessor, history=history)
    assert accessor.reads == []

    with pytest.raises(MediaLimitError, match="content exceeds"):
        await build_user_content("x" * (MAX_USER_CONTENT_BYTES + 1), accessor=accessor)
    assert accessor.reads == []


@pytest.mark.asyncio
async def test_file_references_strip_matching_punctuation_and_preserve_text() -> None:
    png = b"\x89PNG\r\n\x1a\nimage"
    accessor = MediaAccessor({"/workspace/a.png": png})
    prompt = "See @file:/workspace/a.png), then @file:/workspace/a.png], next"
    content = await build_user_content(prompt, accessor=accessor)
    assert isinstance(content, list)
    assert len(_media_blocks(content)) == 2
    assert _text(content) == prompt
    assert len(accessor.reads) == 1


@pytest.mark.asyncio
async def test_media_references_in_markdown_code_are_never_hydrated() -> None:
    accessor = MediaAccessor(
        {"/workspace/a.png": b"\x89PNG\r\n\x1a\nimage", "/workspace/b.png": b"\x89PNG\r\n\x1a\nimage"}
    )
    prompt = (
        "`@file:/workspace/a.png`\n\n"
        "```text\n![x](/workspace/b.png) @file:/workspace/b.png\n```\n"
        "Show ![actual](/workspace/a.png)"
    )
    content = await build_user_content(prompt, accessor=accessor)
    assert [block["type"] for block in _media_blocks(content)] == ["image"]
    assert accessor.reads == [("/workspace/a.png", accessor.reads[0][1])]
    rendered = _text(content)
    assert "`@file:/workspace/a.png`" in rendered
    assert "```text\n![x](/workspace/b.png) @file:/workspace/b.png\n```" in rendered


@pytest.mark.asyncio
async def test_only_user_history_is_scanned_for_media_and_tool_calls_are_preserved() -> None:
    png = b"\x89PNG\r\n\x1a\nimage"
    accessor = MediaAccessor({"/workspace/private.png": png})
    user = LLMMessage(role="user", content="User ![photo](/workspace/private.png)")
    assistant = LLMMessage(
        role="assistant",
        content="Assistant text ![not read](/workspace/private.png)",
        tool_calls=[
            {
                "id": "call-123",
                "name": "read",
                "arguments": json.dumps({"path": "src/app.py", "token": "secret-token"}),
            }
        ],
    )
    tool = LLMMessage(
        role="tool",
        tool_call_id="call-123",
        content=[{"type": "text", "text": "Tool output @file:/workspace/private.png"}],
    )
    content = await build_user_content(
        "Current", accessor=accessor, history=[user, assistant, tool]
    )
    rendered = _text(content)
    assert len(accessor.reads) == 1
    assert len(_media_blocks(content)) == 1
    assert "Assistant text ![not read](/workspace/private.png)" in rendered
    assert "Tool output @file:/workspace/private.png" in rendered
    assert "Historical tool call: name=read id=call-123" in rendered
    assert '"path":"src/app.py"' in rendered
    assert '"token":"[redacted]"' in rendered
    assert "secret-token" not in rendered
    assert "Tool result id=call-123:" in rendered
    assert (
        rendered.index("User: User")
        < rendered.index("Assistant text")
        < rendered.index("Historical tool call: name=read")
        < rendered.index("Tool result id=call-123:")
        < rendered.index("Current user request:")
    )


@pytest.mark.asyncio
async def test_history_tool_call_arguments_are_bounded_per_call() -> None:
    accessor = MediaAccessor()
    call = {
        "id": "call-long",
        "name": "write",
        "arguments": json.dumps(
            {"content": "x" * (MAX_CLAUDE_TOOL_ARGUMENT_BYTES + 100)}
        ),
    }
    content = await build_user_content(
        "continue",
        accessor=accessor,
        history=[LLMMessage(role="assistant", tool_calls=[call])],
    )
    rendered = _text(content)
    assert "Historical tool call: name=write id=call-long" in rendered
    assert "[tool arguments truncated]" in rendered
    assert len(rendered.encode()) < MAX_CLAUDE_TOOL_ARGUMENT_BYTES + 500
