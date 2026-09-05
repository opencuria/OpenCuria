"""Tests for RunnerWorkspaceAccessor against a fake Socket.IO transport."""

from __future__ import annotations

import asyncio
import base64

import pytest

from apps.harness.access.base import ExecChunk
from apps.harness.access.runner_accessor import (
    RunnerAccessorError,
    RunnerWorkspaceAccessor,
    route_harness_chunk,
    route_harness_done,
    route_harness_result,
)


class FakeTransport:
    """Records emits and lets tests inject runner replies."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []
        self.auto_reply = None
        self.event = asyncio.Event()

    async def __call__(self, event: str, payload: dict) -> None:
        self.emitted.append((event, payload))
        self.event.set()
        if self.auto_reply is not None:
            await self.auto_reply(event, payload)


def _accessor(transport: FakeTransport, **kwargs) -> RunnerWorkspaceAccessor:
    return RunnerWorkspaceAccessor("ws-1", emit=transport, **kwargs)


def _request_id(transport: FakeTransport, event: str) -> dict:
    for emitted_event, payload in transport.emitted:
        if emitted_event == event:
            return payload
    raise AssertionError(f"event {event} was not emitted")


async def test_exec_wait_result_and_request_id_correlation() -> None:
    """exec_wait emits harness:exec_wait and resolves the correlated reply."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "exit_code": 0,
                "stdout": "out",
                "stderr": "err",
            }
        )

    transport.auto_reply = auto_reply
    accessor = _accessor(transport)
    result = await accessor.exec_wait(["echo", "hi"])
    assert result.exit_code == 0
    assert result.stdout == "out"
    assert result.stderr == "err"
    payload = _request_id(transport, "harness:exec_wait")
    assert payload["workspace_id"] == "ws-1"
    assert payload["workdir"] == "/workspace"
    assert payload["request_id"]


async def test_exec_stream_chunks_stdout_stderr_and_exit() -> None:
    """exec_stream yields separated stdout/stderr chunks then exit code."""
    transport = FakeTransport()
    accessor = _accessor(transport)

    async def consume():
        chunks: list[ExecChunk] = []
        async for chunk in accessor.exec_stream(["ls"]):
            chunks.append(chunk)
        return chunks

    task = asyncio.create_task(consume())
    await transport.event.wait()
    payload = _request_id(transport, "harness:exec_stream")
    request_id = payload["request_id"]
    route_harness_chunk(
        {
            "request_id": request_id,
            "workspace_id": "ws-1",
            "stream": "stdout",
            "data": "hello",
        }
    )
    route_harness_chunk(
        {
            "request_id": request_id,
            "workspace_id": "ws-1",
            "stream": "stderr",
            "data": "oops",
        }
    )
    route_harness_done(
        {"request_id": request_id, "workspace_id": "ws-1", "exit_code": 3}
    )
    chunks = await task
    assert [c.stream for c in chunks] == ["stdout", "stderr", ""]
    assert chunks[0].data == "hello"
    assert chunks[1].data == "oops"
    assert chunks[2].done is True
    assert chunks[2].exit_code == 3


async def test_runner_error_raises() -> None:
    """Runner-reported errors surface as RunnerAccessorError."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "error": "boom",
            }
        )

    transport.auto_reply = auto_reply
    accessor = _accessor(transport)
    with pytest.raises(RunnerAccessorError, match="boom"):
        await accessor.exec_wait(["false"])


async def test_exec_wait_timeout_sends_cancel() -> None:
    """A timed-out request emits harness:cancel for cleanup."""
    transport = FakeTransport()
    accessor = _accessor(transport, default_timeout=30.0)
    with pytest.raises(TimeoutError, match="timed out"):
        await accessor.exec_wait(["sleep", "5"], timeout=0.02)
    events = [event for event, _ in transport.emitted]
    assert events[0] == "harness:exec_wait"
    assert events[-1] == "harness:cancel"


async def test_exec_stream_cancel_sends_cancel() -> None:
    """Cancelling exec_stream emits harness:cancel for cleanup."""
    transport = FakeTransport()
    accessor = _accessor(transport)
    task = asyncio.create_task(
        _collect(accessor.exec_stream(["sleep", "5"], timeout=30.0))
    )
    await transport.event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = [event for event, _ in transport.emitted]
    assert "harness:cancel" in events


async def _collect(agen):
    return [chunk async for chunk in agen]


async def test_read_write_list_stat_roundtrip() -> None:
    """read/write/list/stat use request_id correlation and sandbox paths."""
    transport = FakeTransport()
    accessor = _accessor(transport)

    async def auto_reply(event: str, payload: dict) -> None:
        rid = payload["request_id"]
        if event == "harness:read_file":
            route_harness_result(
                {
                    "request_id": rid,
                    "workspace_id": "ws-1",
                    "content": base64.b64encode(b"hi").decode(),
                    "size": 2,
                    "truncated": False,
                    "mime": "text/plain",
                }
            )
        elif event == "harness:write_file":
            route_harness_result(
                {"request_id": rid, "workspace_id": "ws-1", "ok": True}
            )
        elif event == "harness:list":
            route_harness_result(
                {
                    "request_id": rid,
                    "workspace_id": "ws-1",
                    "entries": [
                        {
                            "name": "a.txt",
                            "path": "/workspace/a.txt",
                            "is_dir": False,
                            "size": 2,
                        }
                    ],
                }
            )
        elif event == "harness:stat":
            route_harness_result(
                {
                    "request_id": rid,
                    "workspace_id": "ws-1",
                    "path": "/workspace/a.txt",
                    "is_dir": False,
                    "size": 2,
                    "mime": "text/plain",
                }
            )

    transport.auto_reply = auto_reply
    content = await accessor.read_file("/workspace/a.txt")
    assert content.content == b"hi"
    assert content.mime == "text/plain"
    await accessor.write_file("/workspace/a.txt", b"hi")
    entries = await accessor.list_dir("/workspace")
    assert entries[0].name == "a.txt"
    assert entries[0].is_dir is False
    info = await accessor.stat("/workspace/a.txt")
    assert info.size == 2
    assert info.is_dir is False

    with pytest.raises(ValueError, match="under /workspace"):
        await accessor.read_file("/etc/passwd")


async def test_unknown_request_id_returns_false() -> None:
    """Routing helpers return False for unknown request ids."""
    assert route_harness_result({"request_id": "nope"}) is False
    assert route_harness_chunk({"request_id": "nope"}) is False
    assert route_harness_done({"request_id": "nope"}) is False
