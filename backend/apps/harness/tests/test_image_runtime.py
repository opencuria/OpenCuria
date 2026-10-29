"""Image requests through real native HTTP, Codex and Claude processes."""

from __future__ import annotations

import asyncio
import base64
import io
import random
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp import web
from PIL import Image

from apps.harness.engines.base import RunOptions
from apps.harness.image_processing import (
    MAX_IMAGE_EDGE,
    MAX_IMAGE_PREVIEW_BYTES,
    MAX_IMAGE_SOURCE_BYTES,
)
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.providers.base import LLMMessage
from apps.harness.providers.openai_compatible import OpenAICompatibleAdapter
from apps.harness.providers.resolver import StaticModelResolver
from apps.harness.runner import HarnessRunner
from apps.harness.steering import SteeringChannel
from apps.harness.tests.conftest import TINY_PNG, FakeAccessor, make_image
from apps.harness.tests.test_claude_engine import (
    _run_local_cli_fixture,
    require_pinned_cli,
)
from apps.harness.tests.test_codex_core import fixture_run
from apps.harness.tools import default_tool_registry


@dataclass(frozen=True)
class ImageCase:
    """A provider-independent input and expected image count on the wire."""

    prompt: str = "Inspect @file:/workspace/image.png"
    files: dict[str, bytes] = field(default_factory=dict, repr=False)
    history: list[LLMMessage] = field(default_factory=list, repr=False)
    images: int = 0
    denied: bool = False


@cache
def _case(name: str) -> ImageCase:
    if name in {"png", "jpeg", "webp", "gif"}:
        return ImageCase(files={"image.png": make_image(format=name.upper())}, images=1)
    if name == "wide":
        return ImageCase(files={"image.png": make_image((4096, 128))}, images=1)
    if name in {"large_file", "high_entropy"}:
        output = io.BytesIO()
        if name == "large_file":
            image = Image.new("RGB", (2500, 1000), "white")
        else:
            image = Image.frombytes(
                "RGB", (1536, 1024), random.Random(42).randbytes(1536 * 1024 * 3)
            )
        image.save(output, format="PNG", compress_level=0)
        return ImageCase(files={"image.png": output.getvalue()}, images=1)
    if name == "source_limit":
        return ImageCase(files={"image.png": TINY_PNG + b"x" * MAX_IMAGE_SOURCE_BYTES})
    if name == "pixel_limit":
        return ImageCase(files={"image.png": make_image((5001, 5000))})
    if name == "corrupt":
        return ImageCase(files={"image.png": b"\x89PNG\r\n\x1a\ncorrupt"})
    if name == "missing":
        return ImageCase()
    if name == "mixed":
        return ImageCase(
            prompt="Inspect @file:/workspace/image.png and @file:/workspace/broken.png",
            files={"image.png": TINY_PNG, "broken.png": b"broken"},
            images=1,
        )
    if name in {"16_images", "17_images", "100_images"}:
        count = int(name.split("_", 1)[0])
        paths = [f"image-{index}.png" for index in range(count)]
        return ImageCase(
            prompt="Inspect " + " ".join(f"@file:/workspace/{path}" for path in paths),
            files={path: TINY_PNG for path in paths},
            images=count if count <= 16 else 0,
        )
    if name == "history_limit":
        paths = [f"image-{index}.png" for index in range(17)]
        return ImageCase(
            prompt="Inspect "
            + " ".join(f"@file:/workspace/{path}" for path in paths[8:]),
            history=[
                LLMMessage(
                    role="user",
                    content="Previously "
                    + " ".join(f"@file:/workspace/{path}" for path in paths[:8]),
                )
            ],
            files={path: TINY_PNG for path in paths},
        )
    if name == "invalid_history":
        return ImageCase(
            prompt="Continue checking /workspace/image.png",
            history=[
                LLMMessage(
                    role="user",
                    content=[
                        {
                            "type": "image_url",
                            "filename": "/workspace/image.png",
                            "image_url": {"url": "data:image/png;base64,invalid!"},
                        }
                    ],
                )
            ],
        )
    if name == "denied":
        return ImageCase(files={"image.png": TINY_PNG}, denied=True)
    if name == "code_example":
        return ImageCase(
            prompt="Explain `@file:/workspace/image.png`", files={"image.png": TINY_PNG}
        )
    raise AssertionError(name)


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _wire_images(request: dict[str, Any]) -> list[bytes]:
    images = []
    for block in _walk(request.get("messages", request.get("input", []))):
        if block.get("type") == "image" and isinstance(block.get("source"), dict):
            encoded = block["source"]["data"]
        elif block.get("type") in {"image_url", "input_image"}:
            value = block.get("image_url")
            url = value.get("url") if isinstance(value, dict) else value
            assert isinstance(url, str) and url.startswith("data:image/")
            encoded = url.split(",", 1)[1]
        else:
            continue
        payload = base64.b64decode(encoded, validate=True)
        assert len(payload) <= MAX_IMAGE_PREVIEW_BYTES
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            assert max(image.size) <= MAX_IMAGE_EDGE
        images.append(payload)
    return images


async def _native_run(case: ImageCase) -> tuple[Any, list[dict[str, Any]]]:
    requests = []

    async def completion(request: web.Request) -> web.Response:
        requests.append(await request.json())
        return web.json_response(
            {
                "id": "image-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Local done."},
                    }
                ],
            }
        )

    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_post("/v1/chat/completions", completion)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    evaluator = (
        PermissionEvaluator(global_rules={"read": "deny"}) if case.denied else None
    )
    try:
        async with httpx.AsyncClient() as client:
            adapter = OpenAICompatibleAdapter(
                f"http://127.0.0.1:{port}/v1", client=client
            )
            resolver = StaticModelResolver(adapter)
            harness = HarnessRunner(
                model_resolver=resolver.resolve,
                tools=default_tool_registry(),
                accessor=FakeAccessor(
                    files={
                        f"/workspace/{path}": payload
                        for path, payload in case.files.items()
                    }
                ),
                evaluator=evaluator,
            )
            result = await harness.run(
                case.prompt,
                "build",
                "local/image-test",
                "build",
                RunOptions(history=case.history),
            )
        return result, requests
    finally:
        await server.cleanup()


@pytest.mark.parametrize("harness", ["native", "codex", "claude"])
@pytest.mark.parametrize(
    "name",
    [
        "png",
        "jpeg",
        "webp",
        "gif",
        "wide",
        "large_file",
        "high_entropy",
        "source_limit",
        "pixel_limit",
        "corrupt",
        "missing",
        "mixed",
        "16_images",
        "17_images",
        "100_images",
        "history_limit",
        "invalid_history",
        "denied",
        "code_example",
    ],
)
async def test_image_request_reaches_provider_and_run_completes(
    tmp_path: Path, harness: str, name: str
) -> None:
    case = _case(name)
    evaluator = (
        PermissionEvaluator(global_rules={"read": "deny"}) if case.denied else None
    )
    if harness == "native":
        result, requests = await _native_run(case)
    elif harness == "codex":
        result, _, accessor, requests, _, state = await fixture_run(
            tmp_path,
            tool="",
            prompt=case.prompt,
            history=case.history,
            fixture_files=case.files,
            evaluator=evaluator,
        )
        assert state.get("codex_dirty") is False
        assert all(stream.process.returncode is not None for stream in accessor.streams)
        requests = [
            request for request in requests if request.get("model") == "~test/model"
        ]
    else:
        require_pinned_cli()
        result, _, _, requests, _, _, accessor = await _run_local_cli_fixture(
            tmp_path,
            tool_name="",
            tool_arguments={},
            tools=default_tool_registry(),
            prompt=case.prompt,
            history=case.history,
            fixture_files=case.files,
            evaluator=evaluator,
        )
        requests = [request for request in requests if request.get("tools")]
    assert result.output in {"Local done.", "fixture answer"}
    assert requests
    assert len(_wire_images(requests[0])) == case.images
    assert "/workspace/image" in json_text(requests[0])
    if case.images == 0 and name != "code_example":
        assert "Image not loaded" in json_text(requests[0])
    if harness == "codex":
        for path, payload in case.files.items():
            assert accessor.path(f"/workspace/{path}").read_bytes() == payload


def json_text(request: dict[str, Any]) -> str:
    """Collect text without copying image base64 into assertion output."""
    return "\n".join(
        block[key]
        for block in _walk(request.get("messages", request.get("input", [])))
        for key in ("text", "content")
        if isinstance(block.get(key), str)
    )


@pytest.mark.parametrize("name", ["corrupt", "17_images", "large_file"])
async def test_real_codex_image_checkpoint_cold_resume(
    tmp_path: Path, name: str
) -> None:
    case = _case(name)
    result, _, accessor, requests, _, state = await fixture_run(
        tmp_path,
        tool="",
        prompt=case.prompt,
        fixture_files=case.files,
        resume=True,
    )
    assert result.output == "Local done."
    assert state.get("codex_dirty") is False
    assert len(accessor.streams) == 2
    turns = [request for request in requests if request.get("model") == "~test/model"]
    assert len(turns) >= 2
    assert len(_wire_images(turns[0])) == len(_wire_images(turns[-1])) == case.images


async def test_real_codex_resume_keeps_total_image_count_bounded(
    tmp_path: Path,
) -> None:
    paths = [f"image-{index}.png" for index in range(17)]
    result, _, _, requests, _, _ = await fixture_run(
        tmp_path,
        tool="",
        resume=True,
        prompt=" ".join(f"@file:/workspace/{path}" for path in paths[:16]),
        resume_prompt=f"Inspect @file:/workspace/{paths[-1]}",
        fixture_files={path: TINY_PNG for path in paths},
    )
    assert result.output == "Local done."
    turns = [request for request in requests if request.get("model") == "~test/model"]
    assert len(_wire_images(turns[0])) == 16
    assert len(_wire_images(turns[-1])) <= 16
    assert "/workspace/image-16.png" in json_text(turns[-1])


async def test_real_claude_resume_keeps_total_image_count_bounded(
    tmp_path: Path,
) -> None:
    require_pinned_cli()
    initial = _case("16_images")
    result, _, _, _, _, store, _ = await _run_local_cli_fixture(
        tmp_path,
        tool_name="",
        tool_arguments={},
        tools=default_tool_registry(),
        prompt=initial.prompt,
        fixture_files=initial.files,
    )
    resumed, _, _, requests, _, _, _ = await _run_local_cli_fixture(
        tmp_path,
        tool_name="",
        tool_arguments={},
        tools=default_tool_registry(),
        prompt="Inspect @file:/workspace/image-16.png",
        fixture_files={"image-16.png": TINY_PNG},
        store=store,
        external_session_id=result.metadata["external_session_id"],
        history=[LLMMessage(role="user", content=initial.prompt)],
    )
    assert resumed.output == "fixture answer"
    turns = [request for request in requests if request.get("tools")]
    assert turns
    assert len(_wire_images(turns[-1])) <= 16
    assert "Image not loaded" in json_text(turns[-1])
    assert "/workspace/image-16.png" in json_text(turns[-1])


@pytest.mark.parametrize("name", ["corrupt", "17_images", "large_file"])
@pytest.mark.parametrize("prior_images", [False, True])
async def test_real_codex_steering_image_fallback_does_not_end_run(
    tmp_path: Path, name: str, prior_images: bool
) -> None:
    case = _case(name)
    delivered = asyncio.Event()
    accepted = []

    async def deliver(entries: list[Any]) -> None:
        accepted.extend(entry.content for entry in entries)
        delivered.set()

    channel = SteeringChannel(deliver)
    channel.submit(case.prompt)
    first = True

    async def wait_for_steer(body: dict[str, Any]) -> None:
        nonlocal first
        if body.get("model") == "~test/model" and first:
            first = False
            await asyncio.wait_for(delivered.wait(), 10)

    result, _, _, requests, _, state = await fixture_run(
        tmp_path,
        tool="",
        steering=channel,
        request_hook=wait_for_steer,
        prompt=_case("16_images").prompt if prior_images else "Continue the test.",
        fixture_files={
            **(_case("16_images").files if prior_images else {}),
            **case.files,
        },
    )
    assert result.output == "Local done."
    assert state.get("codex_dirty") is False
    assert accepted == [case.prompt]
    assert not channel.pending()
    turns = [request for request in requests if request.get("model") == "~test/model"]
    assert len(turns) >= 2
    assert len(_wire_images(turns[-1])) == (16 if prior_images else case.images)
    assert "/workspace/image" in json_text(turns[-1])


@pytest.mark.parametrize("name", ["corrupt", "17_images", "large_file"])
@pytest.mark.parametrize("prior_images", [False, True])
async def test_real_claude_steering_images_complete_running_turn(
    tmp_path: Path, name: str, prior_images: bool
) -> None:
    require_pinned_cli()
    case = _case(name)
    delivered = asyncio.Event()

    async def deliver(_entries: list[Any]) -> None:
        delivered.set()

    channel = SteeringChannel(deliver)

    async def steer_during_question() -> None:
        channel.submit(case.prompt)
        await asyncio.wait_for(delivered.wait(), 10)
        await asyncio.sleep(0.25)

    result, _, _, requests, _, _, _ = await _run_local_cli_fixture(
        tmp_path,
        tool_name="mcp__opencuria__question",
        tool_arguments={
            "questions": [{"question": "Continue?", "options": [{"label": "Yes"}]}],
        },
        tools=default_tool_registry(),
        prompt=_case("16_images").prompt if prior_images else "Continue the test.",
        fixture_files={
            **(_case("16_images").files if prior_images else {}),
            **case.files,
        },
        steering=channel,
        question_hook=steer_during_question,
    )
    assert result.output == "fixture answer"
    assert not channel.pending()
    turns = [request for request in requests if request.get("tools")]
    # Preview preparation may finish after the first tool-result response.
    assert len(turns) >= 2
    assert len(_wire_images(turns[-1])) == (16 if prior_images else case.images)
    assert "/workspace/image" in json_text(turns[-1])
