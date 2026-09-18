"""Tests for workspace image markdown hydration."""

from __future__ import annotations

import base64

import pytest

from apps.harness.compaction import build_compaction_prompt, estimate_message_tokens
from apps.harness.images import (
    IMAGE_TOKEN_ESTIMATE,
    hydrate_user_messages,
    hydrate_workspace_images,
    resolve_workspace_image_path,
)
from apps.harness.providers.base import LLMMessage
from apps.harness.tests.conftest import FakeAccessor


@pytest.mark.asyncio
async def test_hydrate_workspace_image_adds_data_url() -> None:
    """Image markdown becomes text plus image_url provider parts."""
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 8
    accessor = FakeAccessor(files={"/workspace/cat.png": png_bytes})
    content = "see ![cat](/workspace/cat.png)"
    hydrated = await hydrate_workspace_images(content, accessor)
    assert isinstance(hydrated, list)
    assert hydrated[0] == {"type": "text", "text": "see "}
    assert hydrated[1]["type"] == "image_url"
    url = hydrated[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == png_bytes


@pytest.mark.asyncio
async def test_hydrate_missing_image_keeps_text_only() -> None:
    """Missing files are skipped without raising."""
    accessor = FakeAccessor(files={})
    content = "see ![cat](/workspace/missing.png)"
    hydrated = await hydrate_workspace_images(content, accessor)
    assert hydrated == content


@pytest.mark.asyncio
async def test_hydrate_non_image_markdown_stays_text() -> None:
    """Non-image workspace paths remain plain text."""
    accessor = FakeAccessor(files={"/workspace/a.txt": b"hello"})
    content = "![doc](/workspace/a.txt)"
    hydrated = await hydrate_workspace_images(content, accessor)
    assert hydrated == content


@pytest.mark.asyncio
async def test_hydrate_user_messages_only_touches_user_role() -> None:
    """Only user messages are hydrated in a message list."""
    png_bytes = b"\x89PNG\r\n\x1a\n"
    accessor = FakeAccessor(files={"/workspace/a.png": png_bytes})
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="![a](/workspace/a.png)"),
        LLMMessage(role="assistant", content="ok"),
    ]
    hydrated = await hydrate_user_messages(messages, accessor)
    assert isinstance(hydrated[1].content, list)
    assert hydrated[0].content == "sys"
    assert hydrated[2].content == "ok"


def test_resolve_workspace_image_path_relative_and_absolute() -> None:
    """Relative dests resolve under /workspace; remotes and escapes fail."""
    assert resolve_workspace_image_path("cat.png") == "/workspace/cat.png"
    assert resolve_workspace_image_path("./cat.png") == "/workspace/cat.png"
    assert (
        resolve_workspace_image_path("screenshots/login.png")
        == "/workspace/screenshots/login.png"
    )
    assert (
        resolve_workspace_image_path("/workspace/cat.png")
        == "/workspace/cat.png"
    )
    assert (
        resolve_workspace_image_path('cat.png "kitten"')
        == "/workspace/cat.png"
    )
    assert resolve_workspace_image_path("https://example.com/a.png") is None
    assert resolve_workspace_image_path("../etc/passwd.png") is None
    assert resolve_workspace_image_path("/tmp/x.png") is None
    assert resolve_workspace_image_path("notes.txt") is None
    assert resolve_workspace_image_path("/workspace/clip.mp4") is None


@pytest.mark.asyncio
async def test_hydrate_relative_image_path() -> None:
    """Relative markdown images hydrate from /workspace."""
    png_bytes = b"\x89PNG\r\n\x1a\n"
    accessor = FakeAccessor(files={"/workspace/cat.png": png_bytes})
    hydrated = await hydrate_workspace_images("see ![cat](cat.png)", accessor)
    assert isinstance(hydrated, list)
    assert hydrated[0] == {"type": "text", "text": "see "}
    assert hydrated[1]["type"] == "image_url"

    dotted = await hydrate_workspace_images("![cat](./cat.png)", accessor)
    assert isinstance(dotted, list)
    assert dotted[0]["type"] == "image_url"


@pytest.mark.asyncio
async def test_hydrate_skips_remote_and_escape_images() -> None:
    """HTTPS and sandbox-escaping dests stay plain text."""
    accessor = FakeAccessor(files={"/workspace/cat.png": b"x"})
    remote = "see ![x](https://example.com/a.png)"
    assert await hydrate_workspace_images(remote, accessor) == remote
    escaped = "![x](../etc/passwd.png)"
    assert await hydrate_workspace_images(escaped, accessor) == escaped
    outside = "![x](/tmp/x.png)"
    assert await hydrate_workspace_images(outside, accessor) == outside


def test_compaction_estimates_image_tokens_without_base64() -> None:
    """Multimodal content uses a constant image token estimate."""
    messages = [
        LLMMessage(
            role="user",
            content=[
                {"type": "text", "text": "look"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + ("A" * 10_000),
                    },
                },
            ],
        )
    ]
    assert estimate_message_tokens(messages) == (
        len("look") // 4 + IMAGE_TOKEN_ESTIMATE
    )


def test_images_helpers_map_and_cap_attachments() -> None:
    """Image/file helpers map image_url + file parts, keep filename, cap."""
    from apps.harness.images import (
        TOOL_ATTACHMENT_MAX_CHARS,
        build_tool_message_content,
        select_persisted_tool_attachments,
        tool_message_file_parts,
        tool_message_image_parts,
    )

    attachments = [
        {
            "type": "file",
            "mime": "image/png",
            "url": "data:image/png;base64,AAAA",
            "filename": "cat.png",
        },
        {
            "type": "file",
            "mime": "application/pdf",
            "url": "data:application/pdf;base64,BBBB",
            "filename": "doc.pdf",
        },
    ]
    assert tool_message_image_parts(attachments) == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    ]
    assert tool_message_file_parts(attachments) == [
        {
            "type": "file",
            "mime": "application/pdf",
            "url": "data:application/pdf;base64,BBBB",
            "filename": "doc.pdf",
        }
    ]
    assert tool_message_file_parts([attachments[0]]) == []
    content = build_tool_message_content("PDF read successfully", attachments[1:])
    assert isinstance(content, list)
    assert {"type": "text", "text": "PDF read successfully"} in content
    assert {
        "type": "file",
        "mime": "application/pdf",
        "url": "data:application/pdf;base64,BBBB",
        "filename": "doc.pdf",
    } in content
    assert build_tool_message_content("nothing", []) == "nothing"
    kept = select_persisted_tool_attachments(attachments)
    assert [item["mime"] for item in kept] == ["image/png", "application/pdf"]
    assert [item["filename"] for item in kept] == ["cat.png", "doc.pdf"]
    legacy = [
        {"type": "file", "mime": "image/png", "url": "data:image/png;base64,AAAA"}
    ]
    assert select_persisted_tool_attachments(legacy)[0]["filename"] == ""
    non_string = [
        {
            "type": "file",
            "mime": "image/png",
            "url": "data:image/png;base64,AAAA",
            "filename": 123,
        }
    ]
    assert select_persisted_tool_attachments(non_string)[0]["filename"] == ""
    oversized = [
        {
            "type": "file",
            "mime": "image/png",
            "url": "data:image/png;base64," + ("A" * (TOOL_ATTACHMENT_MAX_CHARS + 1)),
        }
    ]
    assert select_persisted_tool_attachments(oversized) == []


def test_compaction_prompt_replaces_images_with_placeholder() -> None:
    """Compaction serialization must not dump base64 payloads."""
    messages = [
        LLMMessage(
            role="user",
            content=[
                {"type": "text", "text": "see "},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        )
    ]
    prompt = build_compaction_prompt(messages)
    assert "AAAA" not in prompt
    assert "[image]" in prompt
