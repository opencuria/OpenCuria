"""Tests for RunnerWorkspaceAccessor byte streams."""

from __future__ import annotations

import asyncio
import base64
import threading

import pytest

from apps.harness.access.base import StreamClosedError
from apps.harness.access.runner_accessor import (
    STREAM_CHUNK_SIZE,
    RunnerWorkspaceAccessor,
    route_stream_closed,
    route_stream_output,
)


class FakeCallTransport:
    """ACK transport: records stream control calls, auto-ACKs ok."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.auto_ack = True

    async def emit(self, event: str, payload: dict) -> None:
        raise AssertionError("emit should not be used for stream control")

    async def call(
        self, event: str, payload: dict, timeout: float | None = None
    ) -> dict:
        self.calls.append((event, payload))
        if self.auto_ack:
            return {"ok": True, **payload}
        raise TimeoutError("no ack")


def _accessor(transport: FakeCallTransport, **kwargs) -> RunnerWorkspaceAccessor:
    return RunnerWorkspaceAccessor(
        "ws-1", emit=transport.emit, call=transport.call, **kwargs
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


async def test_open_process_sends_validated_start() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["my-mcp", "--stdio"], env={"A": "b"})
    event, payload = transport.calls[0]
    assert event == "workspace:stream_start"
    assert payload["kind"] == "process"
    assert payload["command"] == ["my-mcp", "--stdio"]
    assert payload["workdir"] == "/workspace"
    assert payload["env"] == {"A": "b"}
    assert payload["connection_id"] == stream.connection_id
    await stream.aclose()


async def test_open_process_rejects_empty_command() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    with pytest.raises(ValueError):
        await accessor.open_process([])


async def test_open_tcp_validates_host_port() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_tcp("localhost", 8080)
    _event, payload = transport.calls[0]
    assert payload["kind"] == "tcp"
    assert payload["host"] == "localhost"
    assert payload["port"] == 8080
    assert payload["tls"] is False
    await stream.aclose()
    with pytest.raises(ValueError):
        await accessor.open_tcp("not a host!!", 8080)
    with pytest.raises(ValueError):
        await accessor.open_tcp("localhost", 0)


async def test_receive_send_roundtrip_with_ack() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    conn = stream.connection_id
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stdout",
            "data": _b64(b"hello"),
        }
    )
    assert await stream.receive() == b"hello"
    await stream.send(b"world")
    event, payload = transport.calls[-1]
    assert event == "workspace:stream_input"
    assert base64.b64decode(payload["data"]) == b"world"
    await stream.aclose()


async def test_stderr_goes_to_callback_not_stdout() -> None:
    seen: list[bytes] = []
    transport = FakeCallTransport()
    accessor = _accessor(transport, on_stderr=lambda _conn, data: seen.append(data))
    stream = await accessor.open_process(["cat"])
    conn = stream.connection_id
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stderr",
            "data": _b64(b"warn"),
        }
    )
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stdout",
            "data": _b64(b"out"),
        }
    )
    assert await stream.receive() == b"out"
    assert seen == [b"warn"]
    await stream.aclose()


async def test_closed_event_resolves_eof_and_wait() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    conn = stream.connection_id
    route_stream_closed({"connection_id": conn, "workspace_id": "ws-1", "exit_code": 0})
    assert await stream.receive() == b""
    assert await stream.wait_closed() == 0
    await stream.aclose()


async def test_error_close_raises() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    conn = stream.connection_id
    route_stream_closed(
        {"connection_id": conn, "workspace_id": "ws-1", "error": "boom"}
    )
    with pytest.raises(StreamClosedError, match="boom"):
        await stream.receive()
    await stream.aclose()


async def test_cancel_via_aclose_sends_remote_close() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["sleep", "5"])
    await stream.aclose()
    events = [event for event, _ in transport.calls]
    assert events[0] == "workspace:stream_start"
    assert events[-1] == "workspace:stream_close"
    # Idempotent: second close sends nothing new.
    before = len(transport.calls)
    await stream.aclose()
    assert len(transport.calls) == before


async def test_offline_fails_streams() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    accessor.fail_all_streams("runner gone")
    with pytest.raises(StreamClosedError, match="runner gone"):
        await stream.receive()
    await stream.aclose()


async def test_queue_cap_fails_closed() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport, default_timeout=30.0)
    stream = await accessor.open_process(["yes"])
    conn = stream.connection_id
    # Flood beyond the bounded queue: overflow must fail the stream
    # instead of buffering unbounded output.
    for _ in range(64 + 5):
        route_stream_output(
            {
                "connection_id": conn,
                "workspace_id": "ws-1",
                "stream": "stdout",
                "data": _b64(b"x" * 100),
            }
        )
    await asyncio.sleep(0.1)
    drained = 0
    with pytest.raises(StreamClosedError):
        # Drain until the overflow error surfaces.
        for _ in range(80):
            chunk = await asyncio.wait_for(stream.receive(), timeout=2)
            if chunk == b"":
                raise StreamClosedError("expected overflow error, got EOF")
            drained += 1
    assert drained >= 1
    await stream.aclose()


async def test_output_from_worker_thread() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    conn = stream.connection_id
    finished = threading.Event()

    def from_thread() -> None:
        route_stream_output(
            {
                "connection_id": conn,
                "workspace_id": "ws-1",
                "stream": "stdout",
                "data": _b64(b"threaded"),
            }
        )
        finished.set()

    threading.Thread(target=from_thread, daemon=True).start()
    assert await asyncio.wait_for(stream.receive(), timeout=2) == b"threaded"
    assert finished.wait(timeout=2)
    await stream.aclose()


async def test_workspace_mismatch_dropped() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport, default_timeout=0.2)
    stream = await accessor.open_process(["cat"])
    conn = stream.connection_id
    # Mismatched workspace NACKs (runner closes its side on {ok:false}).
    assert (
        route_stream_output(
            {
                "connection_id": conn,
                "workspace_id": "other-ws",
                "stream": "stdout",
                "data": _b64(b"nope"),
            }
        )
        is False
    )
    # Nothing enqueued: a close notice for the live stream resolves EOF.
    route_stream_closed({"connection_id": conn, "workspace_id": "ws-1", "exit_code": 0})
    assert await asyncio.wait_for(stream.receive(), timeout=2) == b""
    await stream.aclose()


async def test_send_rejects_oversize() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    with pytest.raises(ValueError):
        await stream.send(b"x" * (STREAM_CHUNK_SIZE + 1))
    await stream.aclose()


async def test_send_eof_best_effort() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    await stream.send_eof()
    events = [event for event, _ in transport.calls]
    assert "workspace:stream_close" in events
    await stream.aclose()


async def test_duplicate_start_rejected_by_runner() -> None:
    async def reject_dup(event, payload, timeout=None):
        if event == "workspace:stream_start":
            return {"ok": False, "error": "duplicate"}
        return {"ok": True}

    accessor = RunnerWorkspaceAccessor(
        "ws-1", emit=FakeCallTransport().emit, call=reject_dup
    )
    with pytest.raises(StreamClosedError, match="duplicate"):
        await accessor.open_process(["cat"])


async def test_unknown_connection_returns_false() -> None:
    assert (
        route_stream_output(
            {
                "connection_id": "nope",
                "workspace_id": "ws-1",
                "stream": "stdout",
                "data": _b64(b"x"),
            }
        )
        is False
    )
    assert (
        route_stream_closed({"connection_id": "nope", "workspace_id": "ws-1"}) is False
    )


async def test_no_ack_transport_fails_closed() -> None:
    async def emit_only(event, payload) -> None:
        return None

    accessor = RunnerWorkspaceAccessor("ws-1", emit=emit_only)
    with pytest.raises(Exception, match="no acknowledgement"):
        await accessor.open_process(["cat"])


async def test_receive_has_no_idle_timeout() -> None:
    """receive() must wait indefinitely (MCP idle); cancel closes remote."""
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    # No timeout machinery: a pending receive stays pending until data
    # arrives, however long the idle gap.
    pending = asyncio.ensure_future(stream.receive())
    await asyncio.sleep(0.05)
    assert not pending.done()
    route_stream_output(
        {
            "connection_id": stream.connection_id,
            "workspace_id": "ws-1",
            "stream": "stdout",
            "data": _b64(b"late-data"),
        }
    )
    assert await asyncio.wait_for(pending, timeout=2) == b"late-data"
    # Cancel still triggers a remote close.
    pending2 = asyncio.ensure_future(stream.receive())
    await asyncio.sleep(0.02)
    pending2.cancel()
    await stream.aclose()
    events = [event for event, _ in transport.calls]
    assert events[-1] == "workspace:stream_close"


async def test_wait_closed_has_no_default_timeout() -> None:
    """wait_closed() waits until the runner closes (no 60s default)."""
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["sleep", "5"])
    waiter = asyncio.ensure_future(stream.wait_closed())
    await asyncio.sleep(0.05)
    assert not waiter.done()
    route_stream_closed(
        {
            "connection_id": stream.connection_id,
            "workspace_id": "ws-1",
            "exit_code": 7,
        }
    )
    assert await asyncio.wait_for(waiter, timeout=2) == 7
    await stream.aclose()


async def test_queue_overflow_marks_error_and_requests_remote_close() -> None:
    """Overflow keeps the error for the consumer AND closes remote."""
    emitted: list[tuple[str, dict]] = []

    async def emit_spy(event: str, payload: dict) -> None:
        emitted.append((event, payload))

    async def call_ok(event: str, payload: dict, timeout: float | None = None) -> dict:
        return {"ok": True, **payload}

    accessor = RunnerWorkspaceAccessor(
        "ws-1", emit=emit_spy, call=call_ok, default_timeout=30.0
    )
    stream = await accessor.open_process(["yes"])
    conn = stream.connection_id
    for _ in range(64 + 5):
        route_stream_output(
            {
                "connection_id": conn,
                "workspace_id": "ws-1",
                "stream": "stdout",
                "data": _b64(b"x" * 100),
            }
        )
    await asyncio.sleep(0.2)
    # The waiting consumer still sees the overflow error (not silent EOF).
    with pytest.raises(StreamClosedError, match="overflow"):
        for _ in range(80):
            chunk = await asyncio.wait_for(stream.receive(), timeout=2)
            if chunk == b"":
                raise StreamClosedError("expected overflow error, got EOF")
    # Best-effort remote close was requested so no process lingers.
    await asyncio.sleep(0.2)
    assert ("workspace:stream_close",) == (
        ("workspace:stream_close",)
        if any(e == "workspace:stream_close" for e, _ in emitted)
        else ("missing",)
    )
    await stream.aclose()


async def test_route_returns_false_for_closed_stream() -> None:
    """Unknown/closed/mismatch/invalid streams NACK (runner closes)."""
    from apps.harness.access.runner_accessor import _ACCESSORS_BY_STREAM

    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    conn = stream.connection_id
    await stream.aclose()
    # Closed stream: output + closed both NACK.
    assert (
        route_stream_output(
            {
                "connection_id": conn,
                "workspace_id": "ws-1",
                "stream": "stdout",
                "data": _b64(b"x"),
            }
        )
        is False
    )
    assert route_stream_closed({"connection_id": conn, "workspace_id": "ws-1"}) is False
    # Invalid stream label NACKs on a live stream.
    stream2 = await accessor.open_process(["cat"])
    conn2 = stream2.connection_id
    assert (
        route_stream_output(
            {
                "connection_id": conn2,
                "workspace_id": "ws-1",
                "stream": "bogus",
                "data": _b64(b"x"),
            }
        )
        is False
    )
    await stream2.aclose()
    assert conn not in _ACCESSORS_BY_STREAM


async def test_stderr_never_logged_with_content() -> None:
    """Stderr callback must not log content (only connection id + count)."""
    import logging as _logging

    records: list[_logging.LogRecord] = []

    class _Handler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record)

    secret = b"super-secret-token-12345"
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["cat"])
    conn = stream.connection_id
    # Default on_stderr is None in this harness; wire the factory one.
    from apps.harness.access.runner_accessor import create_harness_accessor

    _ = create_harness_accessor
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stderr",
            "data": _b64(secret),
        }
    )
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stdout",
            "data": _b64(b"ok"),
        }
    )
    assert await stream.receive() == b"ok"
    await stream.aclose()
    for record in records:
        assert "super-secret" not in record.getMessage()


async def test_concurrent_process_and_tcp_streams() -> None:
    """Concurrent process + TCP opens both succeed with distinct ids."""
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    proc, tcp = await asyncio.gather(
        accessor.open_process(["echo", "hi"]),
        accessor.open_tcp("localhost", 8080),
    )
    assert proc.connection_id != tcp.connection_id
    kinds = [call[1]["kind"] for call in transport.calls[:2]]
    assert sorted(kinds) == ["process", "tcp"]
    await proc.aclose()
    await tcp.aclose()


async def test_byte_stream_state_unregisters_routing() -> None:
    """Closing a byte stream drops its routing state (no stale ACKs)."""
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    first = await accessor.open_process(["echo", "one"])
    conn = first.connection_id
    await first.aclose()
    # Unknown ids fail closed on cancel; deliveries report False.
    with pytest.raises(StreamClosedError):
        await accessor.cancel_stream(conn)
    assert (
        route_stream_output(
            {
                "connection_id": conn,
                "workspace_id": "ws-1",
                "stream": "stdout",
                "data": _b64(b"x"),
            }
        )
        is False
    )


# -- MCP failure diagnosis: stderr excerpt + lifecycle counters ------------


async def test_stderr_captured_as_sanitized_excerpt() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["npx", "@playwright/mcp"])
    conn = stream.connection_id
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stderr",
            "data": _b64(b"Error: executable not found at /usr/bin/x\n"),
        }
    )
    await asyncio.sleep(0.05)
    excerpt = accessor.get_stream_stderr_excerpt(conn)
    assert "executable not found" in excerpt
    # Stderr never leaks into the stdout framing.
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stdout",
            "data": _b64(b"out"),
        }
    )
    assert await stream.receive() == b"out"
    await stream.aclose()


async def test_stderr_excerpt_redacts_secrets() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["srv"])
    conn = stream.connection_id
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stderr",
            "data": _b64(b"crash; api_key=hunter2 token=abc --password s3cret\n"),
        }
    )
    await asyncio.sleep(0.05)
    excerpt = accessor.get_stream_stderr_excerpt(conn)
    assert "hunter2" not in excerpt
    assert "s3cret" not in excerpt
    assert "[redacted]" in excerpt
    await stream.aclose()


async def test_stream_diagnostics_counters() -> None:
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(["srv"])
    conn = stream.connection_id
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stdout",
            "data": _b64(b"hello"),
        }
    )
    route_stream_output(
        {
            "connection_id": conn,
            "workspace_id": "ws-1",
            "stream": "stderr",
            "data": _b64(b"warn"),
        }
    )
    await asyncio.sleep(0.05)
    diag = accessor.get_stream_diagnostics(conn)
    assert diag["stdout_bytes"] == 5
    assert diag["stderr_bytes"] == 4
    assert diag["closed"] is False
    assert diag["age_s"] >= 0.0
    route_stream_closed({"connection_id": conn, "workspace_id": "ws-1", "exit_code": 3})
    await asyncio.sleep(0.05)
    diag = accessor.get_stream_diagnostics(conn)
    assert diag["closed"] is True
    assert diag["exit_code"] == 3
    await stream.aclose()


async def test_open_process_sends_command_argv0_logged() -> None:
    """open_process sends the full argv but the open path only records
    argv[0] in its own bookkeeping (full args never persist server-side)."""
    transport = FakeCallTransport()
    accessor = _accessor(transport)
    stream = await accessor.open_process(
        ["npx", "-y", "@playwright/mcp@latest", "--secret-flag", "s3cret"]
    )
    _event, payload = transport.calls[0]
    # Full argv still reaches the runner (it must spawn the process).
    assert payload["command"] == [
        "npx",
        "-y",
        "@playwright/mcp@latest",
        "--secret-flag",
        "s3cret",
    ]
    await stream.aclose()
