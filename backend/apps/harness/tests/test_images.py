"""Tests for workspace image markdown hydration."""

from __future__ import annotations

import base64

import pytest

from apps.harness.images import (
    IMAGE_TOKEN_ESTIMATE,
    hydrate_user_messages,
    hydrate_workspace_images,
)
from apps.harness.compaction import build_compaction_prompt, estimate_message_tokens
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
