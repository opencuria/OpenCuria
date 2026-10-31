"""Pinned release metadata and memory selection without provider inference."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from apps.harness.engines.codex.auth import CodexAuthConfiguration
from apps.harness.engines.codex.catalog import CODEX_VERSION
from apps.harness.engines.codex.configuration import runtime_argv, runtime_settings
from apps.harness.engines.codex.protocol import CodexProtocol
from apps.harness.tests.test_codex_core import PINNED, ByteStream


@asynccontextmanager
async def runtime_client(
    root: Path, *, provider: str = "chatgpt", readonly: bool = False
) -> AsyncIterator[CodexProtocol]:
    """Launch the complete release with production overrides and no real auth."""
    if not PINNED.exists():
        pytest.skip("Pinned Codex binary unavailable; set OPENCURIA_TEST_CODEX_BINARY")
    auth = CodexAuthConfiguration(provider, uuid.uuid4(), {}, base_url="")
    model = "chatgpt/gpt-6.1-sol" if provider == "chatgpt" else "openrouter/test/model"
    settings, _, _ = runtime_settings(auth, model, readonly=readonly)
    process = await asyncio.create_subprocess_exec(
        *runtime_argv(str(PINNED), settings),
        "--strict-config",
        cwd=root,
        env={"HOME": str(root), "CODEX_HOME": str(root), "PATH": "/usr/bin:/bin"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def deny_request(method: str, params: dict) -> dict:
        raise AssertionError(f"Unexpected server request: {method}")

    rpc = CodexProtocol(ByteStream(process), deny_request)
    try:
        await rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "opencuria_runtime_contract", "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await rpc.send({"method": "initialized"})
        yield rpc
    finally:
        await rpc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chatgpt", "openrouter"])
@pytest.mark.parametrize("readonly", [False, True])
async def test_real_runtime_accepts_production_config(tmp_path, provider, readonly):
    async with runtime_client(tmp_path, provider=provider, readonly=readonly) as rpc:
        response = await rpc.request("config/read", {"includeLayers": False})
        config = response["config"]
        assert config["model_provider"] == (
            "openai" if provider == "chatgpt" else "openrouter"
        )
        assert config["approval_policy"] == "never"
        assert config["sandbox_mode"] == (
            "read-only" if readonly else "danger-full-access"
        )


@pytest.mark.asyncio
async def test_real_bundled_model_catalog_matches_pinned_metadata(tmp_path):
    async with runtime_client(tmp_path) as rpc:
        models = []
        cursor = None
        while True:
            page = await rpc.request("model/list", {"cursor": cursor})
            models.extend(page["data"])
            cursor = page.get("nextCursor")
            if cursor is None:
                break
    expected = json.loads(
        (Path(__file__).parents[1] / "engines/codex/models.json").read_text()
    )
    assert expected["runtime_version"] == CODEX_VERSION
    assert expected["models"] == [
        {
            "id": model["id"],
            "name": model["displayName"],
            "reasoning_efforts": [
                effort["reasoningEffort"]
                for effort in model["supportedReasoningEfforts"]
            ],
            "default_effort": model["defaultReasoningEffort"],
            "is_default": model["isDefault"],
        }
        for model in models
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_memory_selection_preserves_legacy_and_defaults_to_v2(
    tmp_path, legacy
):
    for directory in ("memories", "memories_v2"):
        if directory == "memories" and not legacy:
            continue
        target = tmp_path / directory / "memory_summary.md"
        target.parent.mkdir()
        target.write_text(f"Remember {directory}")
    async with runtime_client(tmp_path) as rpc:
        response = await rpc.request(
            "thread/start",
            {
                "cwd": str(tmp_path),
                "ephemeral": True,
                "config": {"memories.generate_memories": False},
            },
        )
        thread_id = response["thread"]["id"]
        result = await rpc.request("thread/memory/read", {"threadId": thread_id})
        expected = tmp_path / ("memories" if legacy else "memories_v2")
        assert result["summary"] == (expected / "memory_summary.md").read_text()
        (expected / "memory_summary.md").unlink()
        assert await rpc.request("thread/memory/read", {"threadId": thread_id}) == {
            "summary": None
        }
