"""Checkpoint size boundaries and bounded notification burst regressions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from apps.harness.engines.codex import protocol
from apps.harness.engines.codex.protocol import (
    CodexEventBufferError,
    CodexProtocol,
)
from apps.harness.engines.codex.session_store import (
    capture,
    home_for,
    restore_snapshot,
    validate_snapshot,
)
from apps.harness.tests.test_codex_core import LocalAccessor, QueueStream


def checkpoint(home: str, payload: bytes) -> dict[str, Any]:
    """Build an allowlisted rollout checkpoint with a real content digest."""
    return {
        "version": 1,
        "home": home,
        "files": [
            {
                "path": "sessions/2026/10/31/rollout-limit.jsonl",
                "data": base64.b64encode(payload).decode(),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }


async def ignore_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded response without contacting a provider."""
    return {}


@pytest.mark.asyncio
async def test_fifty_mib_checkpoint_capture_and_cold_restore(tmp_path: Path) -> None:
    """Exactly 50 MiB survive capture and restore beyond nine transfer pieces."""
    accessor = LocalAccessor(tmp_path / "workspace")
    home = home_for(str(uuid.uuid4()))
    payload = b"x" * (50 * 1024 * 1024)
    record = checkpoint(home, payload)
    uploads = []
    write = accessor.write_file

    async def bounded_write(path: str, content: bytes, mode: int = 0o644) -> None:
        assert len(content) <= 4 * 1024 * 1024
        uploads.append(len(content))
        await write(path, content, mode)

    accessor.write_file = bounded_write
    await restore_snapshot(accessor, home, record)
    assert len(uploads) == 17
    captured = await capture(accessor, home)
    assert captured == record
    assert not list(accessor.path(home).parent.glob("snapshot-*.part"))
    shutil.rmtree(accessor.path(home))
    await restore_snapshot(accessor, home, captured)
    name = record["files"][0]["path"]
    assert accessor.path(home + "/" + name).read_bytes() == payload
    assert not list(accessor.path(home).parent.glob("restore*"))


def test_checkpoint_rejects_more_than_fifty_mib() -> None:
    """A larger allowance must retain the raw aggregate size ceiling."""
    home = home_for(str(uuid.uuid4()))
    record = checkpoint(home, b"x" * (50 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="oversized"):
        validate_snapshot(record, home)


@pytest.mark.asyncio
async def test_notification_burst_preserves_order_and_services_auth_rpc() -> None:
    """A slow event consumer cannot block correlated replies or auth callbacks."""
    stream = QueueStream()

    async def refresh(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "account/chatgptAuthTokens/refresh"
        return {"refreshed": True}

    rpc = CodexProtocol(stream, refresh)
    request = asyncio.create_task(rpc.request("account/read", {}))
    try:
        sent = await stream.output.get()
        events = [
            {"method": "item/agentMessage/delta", "params": {"delta": str(index)}}
            for index in range(1024)
        ]
        frames = [
            *events,
            {"id": "refresh", "method": "account/chatgptAuthTokens/refresh"},
            {"id": sent["id"], "result": {"ok": True}},
        ]
        await stream.input.put(
            b"".join(json.dumps(frame).encode() + b"\n" for frame in frames)
        )
        assert await asyncio.wait_for(request, 1) == {"ok": True}
        assert await asyncio.wait_for(stream.output.get(), 1) == {
            "id": "refresh",
            "result": {"refreshed": True},
        }
        for expected in events:
            assert await asyncio.wait_for(rpc.event(), 1) == expected
        assert rpc.queued_bytes == 0
        assert rpc.failure is None
    finally:
        await rpc.close()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["count", "bytes"])
async def test_notification_overflow_retains_safe_failure_reason(
    monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    """Both buffer ceilings fail pending RPCs with the actual bounded cause."""
    monkeypatch.setattr(protocol, "MAX_EVENTS", 2)
    monkeypatch.setattr(protocol, "MAX_QUEUE_BYTES", 256)
    stream = QueueStream()
    rpc = CodexProtocol(stream, ignore_request)
    request = asyncio.create_task(rpc.request("account/read", {}))
    try:
        await stream.output.get()
        event = {
            "method": "item/agentMessage/delta",
            "params": {"delta": "private-payload" * (32 if limit == "bytes" else 1)},
        }
        frames = [event] * (3 if limit == "count" else 1)
        await stream.input.put(
            b"".join(json.dumps(frame).encode() + b"\n" for frame in frames)
        )
        reason = "queue overflow" if limit == "count" else "byte limit"
        with pytest.raises(CodexEventBufferError, match=reason):
            await asyncio.wait_for(request, 1)
        assert "private-payload" not in str(rpc.failure)
        assert rpc.events.qsize() <= 2
        assert rpc.queued_bytes <= 256
    finally:
        await rpc.close()
        await asyncio.gather(request, return_exceptions=True)
