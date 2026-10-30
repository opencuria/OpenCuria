"""Shared image input behavior across native, Claude and Codex harnesses."""

from __future__ import annotations

import asyncio
import base64
import io
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from PIL import Image

from apps.harness.engines.base import RunOptions
from apps.harness.engines.codex.media import user_input
from apps.harness.image_processing import (
    MAX_IMAGE_EDGE,
    MAX_IMAGE_PREVIEW_BYTES,
    MAX_IMAGE_SOURCE_BYTES,
    prepare_image,
)
from apps.harness.media import context as media
from apps.harness.providers.base import LLMMessage
from apps.harness.tests.conftest import TINY_PNG, make_image
from apps.harness.tests.test_claude_context import MediaAccessor
from apps.harness.tests.test_runner_loop import FakeProvider, _runner, _text_step


async def _input(
    harness: str,
    prompt: str,
    accessor: MediaAccessor,
    *,
    history: list[LLMMessage] | None = None,
    authorize: Callable[[str], Awaitable[bool]] | None = None,
) -> list[dict[str, Any]]:
    if harness == "native":
        messages = await media.hydrate_image_messages(
            [*(history or []), LLMMessage(role="user", content=prompt)],
            accessor,
            authorize_read=authorize,
        )
        return [
            part
            for message in messages
            for part in (
                [{"type": "text", "text": message.content}]
                if isinstance(message.content, str)
                else message.content or []
            )
        ]
    if harness == "codex":
        return await user_input(
            prompt,
            accessor=accessor,
            opts=RunOptions(history=history or [], authorize_file_read=authorize),
            include_history=True,
        )
    content = await media.build_user_content(
        prompt, accessor=accessor, history=history, authorize_read=authorize
    )
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


def _text(blocks: list[dict[str, Any]]) -> str:
    return "".join(block.get("text", "") for block in blocks)


def _images(blocks: list[dict[str, Any]]) -> list[bytes]:
    images = []
    for block in blocks:
        if block["type"] == "image_url":
            encoded = block["image_url"]["url"].split(",", 1)[1]
        elif block["type"] == "image":
            encoded = (
                block["source"]["data"]
                if "source" in block
                else block["url"].split(",", 1)[1]
            )
        else:
            continue
        images.append(base64.b64decode(encoded, validate=True))
    return images


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
@pytest.mark.parametrize("reference", ["@file:/workspace/a.png", "![a](a.png)"])
async def test_harnesses_share_valid_image_previews(
    harness: str, reference: str
) -> None:
    accessor = MediaAccessor({"/workspace/a.png": TINY_PNG})
    blocks = await _input(harness, f"Inspect {reference}", accessor)
    assert _images(blocks) == [TINY_PNG]
    assert reference in _text(blocks)
    assert accessor.no_follow_reads == [("/workspace/a.png", MAX_IMAGE_SOURCE_BYTES)]


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
@pytest.mark.parametrize("failure", ["missing", "corrupt", "too_large", "read_error"])
async def test_image_failures_always_keep_path(harness: str, failure: str) -> None:
    payload = {
        "corrupt": b"\x89PNG\r\n\x1a\ninvalid",
        "too_large": b"x" * (MAX_IMAGE_SOURCE_BYTES + 1),
    }.get(failure, TINY_PNG)
    accessor = MediaAccessor(
        {} if failure == "missing" else {"/workspace/a.png": payload}
    )
    if failure == "read_error":

        async def fail_read(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("private transport failure")

        accessor.read_file_no_follow = fail_read
    blocks = await _input(harness, "Inspect @file:/workspace/a.png", accessor)
    assert not _images(blocks)
    assert "/workspace/a.png" in _text(blocks)
    assert "Image not loaded" in _text(blocks)
    assert "private transport" not in _text(blocks)


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
async def test_many_images_use_only_paths_without_reads(harness: str) -> None:
    paths = [
        f"/workspace/image-{index}.png"
        for index in range(media.MAX_USER_IMAGE_BLOCKS + 1)
    ]
    accessor = MediaAccessor({path: TINY_PNG for path in paths})
    blocks = await _input(
        harness,
        " ".join(f"@file:{path}" for path in paths[8:]),
        accessor,
        history=[
            LLMMessage(
                role="user", content=" ".join(f"@file:{path}" for path in paths[:8])
            )
        ],
    )
    assert not _images(blocks)
    assert accessor.reads == []
    assert all(path in _text(blocks) for path in paths)
    assert "too many images" in _text(blocks)


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
async def test_denied_image_read_keeps_path_without_opening_file(harness: str) -> None:
    accessor = MediaAccessor({"/workspace/a.png": TINY_PNG})
    authorized = []

    async def deny(path: str) -> bool:
        authorized.append(path)
        return False

    blocks = await _input(harness, "@file:/workspace/a.png", accessor, authorize=deny)
    assert not _images(blocks)
    assert "/workspace/a.png" in _text(blocks)
    assert authorized == ["/workspace/a.png"]
    assert accessor.reads == []


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
async def test_large_image_is_reduced_without_modifying_original(harness: str) -> None:
    output = io.BytesIO()
    Image.new("RGB", (2500, 1000), "white").save(output, format="PNG", compress_level=0)
    original = output.getvalue()
    assert len(original) > media.MAX_CLAUDE_IMAGE_BASE64_CHARS * 3 // 4
    accessor = MediaAccessor({"/workspace/a.png": original})
    blocks = await _input(harness, "@file:/workspace/a.png", accessor)
    (preview,) = _images(blocks)
    assert len(preview) <= MAX_IMAGE_PREVIEW_BYTES
    with Image.open(io.BytesIO(preview)) as image:
        assert max(image.size) <= MAX_IMAGE_EDGE
        assert image.width / image.height == pytest.approx(2.5, rel=0.01)
        image.load()
    assert accessor.files["/workspace/a.png"] == original
    assert "/workspace/a.png" in _text(blocks)


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
async def test_image_cancellation_still_stops_run(harness: str) -> None:
    accessor = MediaAccessor()

    async def cancelled(*_args: Any, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    accessor.read_file_no_follow = cancelled
    with pytest.raises(asyncio.CancelledError):
        await _input(harness, "@file:/workspace/a.png", accessor)


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
async def test_aggregate_image_budget_falls_back_without_losing_paths(
    harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded_size = len(base64.b64encode(TINY_PNG))
    monkeypatch.setattr(media, "MAX_CLAUDE_IMAGE_BASE64_CHARS", encoded_size)
    paths = [f"/workspace/{index}.png" for index in range(3)]
    blocks = await _input(
        harness,
        " ".join(f"@file:{path}" for path in paths),
        MediaAccessor({path: TINY_PNG for path in paths}),
    )
    assert 0 < len(_images(blocks)) < len(paths)
    assert all(path in _text(blocks) for path in paths)
    assert "Image not loaded" in _text(blocks)


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
async def test_one_bad_image_keeps_other_previews(harness: str) -> None:
    blocks = await _input(
        harness,
        "@file:/workspace/bad.png @file:/workspace/good.png",
        MediaAccessor(
            {"/workspace/bad.png": b"broken", "/workspace/good.png": TINY_PNG}
        ),
    )
    assert _images(blocks) == [TINY_PNG]
    assert "/workspace/bad.png" in _text(blocks)
    assert "/workspace/good.png" in _text(blocks)


async def test_preview_preserves_transparency_and_normalizes_animation() -> None:
    output = io.BytesIO()
    Image.new("RGBA", (MAX_IMAGE_EDGE + 1, 10), (1, 2, 3, 0)).save(output, format="PNG")
    preview = await prepare_image(output.getvalue())
    with Image.open(io.BytesIO(preview.content)) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0
    animated = await prepare_image(make_image(format="GIF"))
    assert animated.mime == "image/png"


async def test_invalid_inline_history_image_does_not_abort_current_turn() -> None:
    blocks = await _input(
        "codex",
        "Continue",
        MediaAccessor(),
        history=[
            LLMMessage(
                role="user",
                content=[
                    {
                        "type": "image_url",
                        "filename": "/workspace/a.png",
                        "image_url": {"url": "data:image/png;base64,invalid!"},
                    },
                ],
            )
        ],
    )
    assert not _images(blocks)
    assert "Continue" in _text(blocks)
    assert "/workspace/a.png" in _text(blocks)


def test_tool_image_limit_retains_roles_calls_and_file_references() -> None:
    messages = [LLMMessage(role="user", content="@file:/workspace/a.png")]
    for index in range(media.MAX_USER_IMAGE_BLOCKS + 1):
        messages.append(
            LLMMessage(
                role="tool",
                tool_call_id=f"call-{index}",
                content=[
                    {"type": "text", "text": "/workspace/a.png"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            )
        )
    bounded = media.limit_image_messages(messages)
    assert [message.tool_call_id for message in bounded] == [
        message.tool_call_id for message in messages
    ]
    assert bounded[0] == messages[0]
    assert all(not _images(message.content) for message in bounded[1:])
    assert all("/workspace/a.png" in _text(message.content) for message in bounded[1:])
    assert messages[-1].content[-1]["type"] == "image_url"


@pytest.mark.parametrize("count", [1, media.MAX_USER_IMAGE_BLOCKS + 1])
async def test_native_run_completes_with_unavailable_images(count: int) -> None:
    provider = FakeProvider([_text_step("completed")])
    runner, opts = _runner(provider, [], files={"/workspace/a.png": b"corrupt"})
    result = await runner.run(
        " ".join("@file:/workspace/a.png" for _ in range(count)),
        "build",
        "m",
        "build",
        opts,
    )
    assert result.output == "completed"
    blocks = [
        part
        for message in provider.messages[0]
        if message.role == "user"
        for part in (
            [{"type": "text", "text": message.content}]
            if isinstance(message.content, str)
            else message.content
        )
    ]
    assert not _images(blocks)
    assert "/workspace/a.png" in _text(blocks)


async def test_native_image_permissions_apply_on_each_run() -> None:
    from apps.harness.permissions.evaluator import PermissionEvaluator

    provider = FakeProvider([_text_step("first"), _text_step("second")])
    runner, opts = _runner(provider, [], files={"/workspace/a.png": TINY_PNG})
    await runner.run("@file:/workspace/a.png", "build", "m", "build", opts)
    assert opts.authorize_file_read is None
    runner.evaluator = PermissionEvaluator(global_rules={"read": "deny"})
    await runner.run("@file:/workspace/a.png", "build", "m", "build", opts)
    assert isinstance(provider.messages[0][-1].content, list)
    assert isinstance(provider.messages[1][-1].content, str)
    assert "Image not loaded" in provider.messages[1][-1].content
