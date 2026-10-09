"""Workspace stream transport tests, independent of the Claude SDK install."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from apps.harness.engines.claude.transport import ClaudeWorkspaceTransport


class FakeStream:
    """Tiny fake byte stream recording exactly the remote interactions."""

    def __init__(self, chunks: list[bytes], exit_code: int | None = 0) -> None:
        self.chunks = list(chunks)
        self.exit_code = exit_code
        self.sent: list[bytes] = []
        self.eof = 0
        self.closed = 0

    async def receive(self) -> bytes:
        await asyncio.sleep(0)
        return self.chunks.pop(0) if self.chunks else b""

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def send_eof(self) -> None:
        self.eof += 1

    async def wait_closed(self) -> int | None:
        return self.exit_code

    async def aclose(self) -> None:
        self.closed += 1


class FakeAccessor:
    """Accessor seam for remote stream opening."""

    workspace_id = "workspace"

    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream
        self.opened: dict[str, Any] | None = None

    async def open_process(self, command, workdir, env, timeout=None, **kwargs):
        self.opened = {
            "command": command,
            "workdir": workdir,
            "env": env,
            "timeout": timeout,
            **kwargs,
        }
        return self.stream


@pytest.mark.asyncio
async def test_transport_frames_ndjson_across_chunks_and_bounds_writes() -> None:
    wire = b'{"type":"one"}\n{"type":"two","text":"a b"}\n'
    stream = FakeStream([wire[:11], wire[11:29], wire[29:]])
    accessor = FakeAccessor(stream)
    transport = ClaudeWorkspaceTransport(
        accessor,
        ["/opt/claude"],
        cwd="/workspace",
        env={"CLAUDE_CONFIG_DIR": "/workspace/.opencuria"},
    )
    await transport.connect()
    messages = [message async for message in transport.read_messages()]
    assert messages == [{"type": "one"}, {"type": "two", "text": "a b"}]
    assert accessor.opened["command"] == ["/opt/claude"]

    await transport.write("x" * (64 * 1024 + 5))
    assert [len(chunk) for chunk in stream.sent] == [64 * 1024, 5]
    await transport.end_input()
    await transport.close()
    await transport.close()
    assert stream.eof == 1
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_transport_ignores_diagnostics_and_fails_nonzero_exit() -> None:
    stream = FakeStream([b"diagnostic\n", b'{"ok":true}\n'], exit_code=7)
    transport = ClaudeWorkspaceTransport(
        FakeAccessor(stream), ["/claude"], cwd="/workspace", env={}
    )
    await transport.connect()
    with pytest.raises(Exception, match="exited unsuccessfully"):
        assert [item async for item in transport.read_messages()] == [{"ok": True}]
    await transport.close()


def test_transport_rejects_non_object_json() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        ClaudeWorkspaceTransport._parse_line("[]")


def test_transport_parses_crlf_json_line() -> None:
    assert ClaudeWorkspaceTransport._parse_line('{"type":"x"}\r') == {"type": "x"}
