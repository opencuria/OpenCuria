"""Tests for the MCP stdio adapter over fake WorkspaceByteStreams.

End-to-end initialize/list_tools/call_tool against a minimal fake
JSON-RPC MCP server, plus fragmented lines, invalid/oversize JSON,
and cancellation/close semantics.
"""

from __future__ import annotations

import asyncio
import json

import anyio
import mcp.types as types
import pytest
from mcp.client.session import ClientSession
from mcp.shared.message import SessionMessage

from apps.harness.mcp_client.stdio import (
    MAX_STDIO_LINE_BYTES,
    workspace_stdio_client,
)


class FakeByteStream:
    """In-memory WorkspaceByteStream double (client <-> fake server)."""

    def __init__(self) -> None:
        self.to_client: asyncio.Queue = asyncio.Queue()
        self.to_server: asyncio.Queue = asyncio.Queue()
        self.eof_sent = False
        self.closed = False

    async def receive(self) -> bytes:
        item = await self.to_client.get()
        return item

    async def send(self, data: bytes) -> None:
        self.to_server.put_nowait(bytes(data))

    async def send_eof(self) -> None:
        self.eof_sent = True

    async def aclose(self) -> None:
        self.closed = True

    async def wait_closed(self):  # pragma: no cover - unused in tests
        return 0


class FakeAccessor:
    """Accessor double recording open_process calls."""

    def __init__(self, stream: FakeByteStream) -> None:
        self.stream = stream
        self.opened: list[tuple] = []

    async def open_process(self, command, workdir="/workspace", env=None, timeout=None):
        self.opened.append((list(command), workdir, dict(env or {}), timeout))
        return self.stream


def _result(mid, payload):
    return {"jsonrpc": "2.0", "id": mid, "result": payload}


async def _fake_mcp_server(stream: FakeByteStream, tools: list[dict] | None = None):
    """Minimal JSON-RPC MCP server answering initialize/list/call."""
    tools = tools if tools is not None else [
        {
            "name": "echo",
            "description": "echo it",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        }
    ]
    buf = b""
    while True:
        chunk = await stream.to_server.get()
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                req = json.loads(line)
            except Exception:
                continue
            mid = req.get("id")
            method = req.get("method")
            if method == "notifications/initialized":
                continue
            if mid is None:
                continue
            if method == "initialize":
                resp = _result(
                    mid,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1"},
                    },
                )
            elif method == "tools/list":
                resp = _result(mid, {"tools": tools})
            elif method == "tools/call":
                args = (req.get("params") or {}).get("arguments", {})
                resp = _result(
                    mid,
                    {"content": [{"type": "text", "text": f"echo:{args.get('text')}"}]},
                )
            else:
                resp = _result(mid, {})
            stream.to_client.put_nowait((json.dumps(resp) + "\n").encode())


@pytest.mark.asyncio
async def test_stdio_end_to_end_initialize_list_call():
    stream = FakeByteStream()
    accessor = FakeAccessor(stream)
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_fake_mcp_server, stream)
        async with workspace_stdio_client(
            accessor, ["fake-mcp", "--flag"], cwd="/workspace", env={"A": "b"}
        ) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "fake"
                listed = await session.list_tools()
                assert [t.name for t in listed.tools] == ["echo"]
                result = await session.call_tool("echo", {"text": "hi"})
                assert result.content[0].text == "echo:hi"
        task_group.cancel_scope.cancel()
    assert accessor.opened == [
        (["fake-mcp", "--flag"], "/workspace", {"A": "b"}, None)
    ]
    assert stream.eof_sent is True
    assert stream.closed is True


@pytest.mark.asyncio
async def test_stdio_fragmented_lines_reassembled():
    stream = FakeByteStream()
    accessor = FakeAccessor(stream)
    response = (
        json.dumps(
            _result(
                1,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "frag", "version": "1"},
                },
            )
        )
        + "\n"
    ).encode()
    halves = [response[:10], response[10:25], response[25:], b""]

    async def _feed():
        for part in halves:
            await stream.to_client.put(part)

    async def _answer_calls():
        # Answer the initialized notification + any tool calls generically.
        buf = b""
        while True:
            chunk = await stream.to_server.get()
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                req = json.loads(line)
                if req.get("method") == "notifications/initialized":
                    continue

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_feed)
        task_group.start_soon(_answer_calls)
        async with workspace_stdio_client(accessor, ["frag"]) as (read, write):
            message = await read.receive()
            assert isinstance(message, SessionMessage)
            assert message.message.root.id == 1
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_stdio_invalid_json_delivered_as_exception():
    stream = FakeByteStream()
    accessor = FakeAccessor(stream)
    good = (
        json.dumps(
            _result(
                "req-7",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "g", "version": "1"},
                },
            )
        )
        + "\n"
    ).encode()

    async def _feed():
        await stream.to_client.put(b"this is not json\n")
        await stream.to_client.put(good)
        await stream.to_client.put(b"")

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_feed)
        async with workspace_stdio_client(accessor, ["bad"]) as (read, write):
            first = await read.receive()
            assert isinstance(first, Exception)
            second = await read.receive()
            assert isinstance(second, SessionMessage)
            assert second.message.root.id == "req-7"
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_stdio_framing_failure_closes_without_drain():
    """After a framing failure the reader stops (no endless drain)."""
    stream = FakeByteStream()
    accessor = FakeAccessor(stream)
    big_line = b"x" * (MAX_STDIO_LINE_BYTES + 16) + b"\n"

    async def _feed():
        await stream.to_client.put(big_line)
        # A babbling server keeps sending after the failure: the fixed
        # reader must NOT drain this — it breaks out instead.
        await stream.to_client.put(b"after-failure-payload\n")
        await stream.to_client.put(b"")

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_feed)
        async with workspace_stdio_client(accessor, ["big"]) as (read, write):
            with anyio.fail_after(10):
                message = await read.receive()
                assert isinstance(message, Exception)
                assert "8MiB" in str(message)
        task_group.cancel_scope.cancel()
    assert stream.closed is True


@pytest.mark.asyncio
async def test_stdio_cancellation_closes_stream():
    stream = FakeByteStream()
    accessor = FakeAccessor(stream)
    entered = anyio.Event()
    release = anyio.Event()

    async def _run_client():
        async with workspace_stdio_client(accessor, ["hang"]) as (read, write):
            entered.set()
            await release.wait()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_run_client)
        await entered.wait()
        task_group.cancel_scope.cancel()
    assert stream.eof_sent is True
    assert stream.closed is True


@pytest.mark.asyncio
async def test_stdio_writer_splits_large_json_rpc_frames():
    from apps.harness.access.runner_accessor import STREAM_CHUNK_SIZE

    class BoundedStream(FakeByteStream):
        async def send(self, data: bytes) -> None:
            assert 0 < len(data) <= STREAM_CHUNK_SIZE
            await super().send(data)

    stream = BoundedStream()
    accessor = FakeAccessor(stream)
    large_text = "x" * (STREAM_CHUNK_SIZE * 2)
    async with workspace_stdio_client(accessor, ["cap"]) as (_read, write):
        request = types.JSONRPCRequest(
            jsonrpc="2.0", id=1, method="ping", params={"text": large_text}
        )
        await write.send(SessionMessage(request))
        chunks = []
        while True:
            chunk = await asyncio.wait_for(stream.to_server.get(), 2)
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
        assert len(chunks) >= 3
        assert json.loads(b"".join(chunks))["params"]["text"] == large_text


@pytest.mark.asyncio
async def test_stdio_writer_serialization_format():
    stream = FakeByteStream()
    accessor = FakeAccessor(stream)

    async def _capture_one():
        await stream.to_server.get()
        stream.to_client.put_nowait(b"")

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_capture_one)
        async with workspace_stdio_client(accessor, ["cap"]) as (read, write):
            request = types.JSONRPCRequest(
                jsonrpc="2.0",
                id=1,
                method="ping",
                params=None,
            )
            await write.send(SessionMessage(request))
            raw = await stream.to_server.get()
            payload = json.loads(raw.decode())
            # by_alias + exclude_none: no null params key.
            assert payload["method"] == "ping"
            assert "params" not in payload
            assert raw.endswith(b"\n")
        task_group.cancel_scope.cancel()
