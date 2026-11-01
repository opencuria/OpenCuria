"""Fragmented Codex records retain ordering, bounds and linear scan work."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from apps.harness.engines.codex import protocol
from apps.harness.engines.codex.protocol import CodexProtocol, CodexProtocolError


class ByteStream:
    """Keep the reader open until a test supplies data or closes the protocol."""

    def __init__(self) -> None:
        self.input: asyncio.Queue[bytes] = asyncio.Queue()

    async def receive(self) -> bytes:
        return await self.input.get()

    async def send_eof(self) -> None:
        pass

    async def wait_closed(self) -> int:
        return 0

    async def aclose(self) -> None:
        pass


@pytest.fixture
async def rpc() -> AsyncIterator[CodexProtocol]:
    """Always cancel and join the protocol reader, including assertion failures."""
    instance = CodexProtocol(ByteStream(), None)
    try:
        yield instance
    finally:
        await instance.close()
        assert instance.reader.done()
        assert not instance.handlers


def frame(text: str) -> bytes:
    """Encode one notification without ASCII escaping multibyte characters."""
    return json.dumps(
        {"method": "item/completed", "params": {"text": text}}, ensure_ascii=False
    ).encode()


@pytest.mark.parametrize("chunk_size", [1, 3, 31, 4096, 65536])
async def test_fragmented_and_coalesced_records_keep_order(
    rpc: CodexProtocol, chunk_size: int
) -> None:
    records = [frame("é🙂" * 1200), frame("second"), frame("third")]
    wire = b"\n\r\n" + b"\r\n\n".join(records) + b"\n"
    for start in range(0, len(wire), chunk_size):
        await rpc.stream.input.put(wire[start : start + chunk_size])
    for record in records:
        assert await asyncio.wait_for(rpc.event(), 2) == json.loads(record)
    assert rpc.queued_bytes == 0
    assert rpc.failure is None


async def test_frame_at_limit_accepts_newline_in_later_chunk(
    rpc: CodexProtocol, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = frame("é🙂")
    monkeypatch.setattr(protocol, "MAX_FRAME", len(record))
    await rpc.stream.input.put(record[:-1])
    # Finish one record and start the next in a single read.
    await rpc.stream.input.put(record[-1:] + b"\n" + record[:5])
    await rpc.stream.input.put(record[5:] + b"\n")
    for _ in range(2):
        assert await asyncio.wait_for(rpc.event(), 1) == json.loads(record)
    assert rpc.failure is None


@pytest.mark.parametrize("terminated", [False, True])
async def test_fragmented_oversize_frame_closes_protocol(
    rpc: CodexProtocol, monkeypatch: pytest.MonkeyPatch, terminated: bool
) -> None:
    record = frame("é🙂")
    monkeypatch.setattr(protocol, "MAX_FRAME", len(record) - 1)
    await rpc.stream.input.put(record[:-1])
    await rpc.stream.input.put(record[-1:] + (b"\n" if terminated else b""))
    await asyncio.wait_for(rpc.reader, 1)
    with pytest.raises(CodexProtocolError, match="frame exceeds limit"):
        await rpc.event()
    assert rpc.events.empty()


@pytest.mark.parametrize("tail", [b"\xff\n", b"[]\n", b'{"params":[] ,"method":"x"}\n'])
async def test_invalid_record_after_valid_prefix_fails_safely(
    rpc: CodexProtocol, tail: bytes
) -> None:
    await rpc.stream.input.put(frame("valid") + b"\n" + tail)
    await asyncio.wait_for(rpc.reader, 1)
    assert isinstance(rpc.failure, CodexProtocolError)
    assert rpc.events.qsize() == 1
    with pytest.raises(CodexProtocolError):
        await rpc.event()


async def test_eof_does_not_dispatch_unterminated_record(rpc: CodexProtocol) -> None:
    await rpc.stream.input.put(frame("unfinished"))
    await rpc.stream.input.put(b"")
    await asyncio.wait_for(rpc.reader, 1)
    assert rpc.events.empty()
    with pytest.raises(CodexProtocolError, match="stream ended"):
        await rpc.event()


async def test_fragmented_frame_scan_work_is_linear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Count bytes searched instead of relying on noisy wall-clock thresholds."""
    searched = 0

    class MeasuredBuffer(bytearray):
        def find(self, sub: bytes, start: int = 0) -> int:
            nonlocal searched
            index = super().find(sub, start)
            searched += (index + len(sub) if index >= 0 else len(self)) - start
            return index

    monkeypatch.setattr(protocol, "bytearray", MeasuredBuffer, raising=False)
    record = frame("x" * (128 * 1024))
    wire = record + b"\n" + frame("last") + b"\n"
    rpc = CodexProtocol(ByteStream(), None)
    try:
        for start in range(0, len(wire), 128):
            await rpc.stream.input.put(wire[start : start + 128])
        assert await asyncio.wait_for(rpc.event(), 2) == json.loads(record)
        assert (await asyncio.wait_for(rpc.event(), 1))["params"]["text"] == "last"
        assert 0 < searched <= len(wire)
    finally:
        await rpc.close()
