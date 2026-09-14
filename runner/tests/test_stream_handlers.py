"""Tests for runner workspace stream handlers."""

from __future__ import annotations

import asyncio
import base64
import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.models import WorkspaceInfo
from src.service import WorkspaceService


class FakeStreamRuntime:
    def __init__(self) -> None:
        self.spawned: list[dict] = []

    async def spawn_process(self, instance_id, command, workdir=None, env=None):
        from src.runtime.base import ProcessHandle

        self.spawned.append({"command": list(command)})
        return ProcessHandle(instance_id=instance_id, handle=object())

    async def process_read(self, handle, stream="stdout", size=65536):
        return b""

    async def process_write(self, handle, data: bytes) -> None:
        handle.metadata.setdefault("written", []).append(bytes(data))

    async def process_write_eof(self, handle) -> None:
        handle.metadata["eof"] = True

    async def process_wait(self, handle) -> int:
        return 0

    async def process_close(self, handle) -> None:
        handle.closed = True


def _setup():
    runtime = FakeStreamRuntime()
    service = WorkspaceService(
        runtimes={"docker": runtime}, settings=RunnerSettings()
    )
    ws_id = uuid.uuid4()
    service._cache[ws_id] = WorkspaceInfo(
        workspace_id=ws_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    interface = WebSocketInterface(service, RunnerSettings())
    interface._sio.emit = AsyncMock()
    interface._sio.call = AsyncMock(return_value={"ok": True})
    handlers = interface._sio.handlers["/"]
    return service, runtime, ws_id, interface, handlers


class StreamHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_process_returns_ack(self) -> None:
        _svc, _rt, ws_id, _if, handlers = _setup()
        result = await handlers["workspace:stream_start"](
            {
                "connection_id": "c1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["my-mcp", "--stdio"],
                "workdir": "/workspace",
                "env": {"A": "b"},
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["connection_id"], "c1")

    async def test_start_duplicate_id_rejected(self) -> None:
        _svc, _rt, ws_id, _if, handlers = _setup()
        payload = {
            "connection_id": "dup",
            "workspace_id": str(ws_id),
            "kind": "process",
            "command": ["sleep", "1"],
        }
        first = await handlers["workspace:stream_start"](payload)
        self.assertTrue(first["ok"])
        second = await handlers["workspace:stream_start"](payload)
        self.assertFalse(second["ok"])
        self.assertIn("Duplicate", second["error"])

    async def test_start_unknown_kind_rejected(self) -> None:
        _svc, _rt, ws_id, _if, handlers = _setup()
        result = await handlers["workspace:stream_start"](
            {
                "connection_id": "k1",
                "workspace_id": str(ws_id),
                "kind": "mcp",
            }
        )
        self.assertFalse(result["ok"])

    async def test_input_validates_base64_and_workspace(self) -> None:
        _svc, _rt, ws_id, _if, handlers = _setup()
        await handlers["workspace:stream_start"](
            {
                "connection_id": "i1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["cat"],
            }
        )
        good = base64.b64encode(b"hello").decode()
        result = await handlers["workspace:stream_input"](
            {
                "connection_id": "i1",
                "workspace_id": str(ws_id),
                "data": good,
            }
        )
        self.assertTrue(result["ok"])
        bad = await handlers["workspace:stream_input"](
            {
                "connection_id": "i1",
                "workspace_id": str(ws_id),
                "data": "!!!not-base64!!!",
            }
        )
        self.assertFalse(bad["ok"])
        mismatch = await handlers["workspace:stream_input"](
            {
                "connection_id": "i1",
                "workspace_id": str(uuid.uuid4()),
                "data": good,
            }
        )
        self.assertFalse(mismatch["ok"])
        oversize = base64.b64encode(b"x" * (64 * 1024 + 1)).decode()
        too_big = await handlers["workspace:stream_input"](
            {
                "connection_id": "i1",
                "workspace_id": str(ws_id),
                "data": oversize,
            }
        )
        self.assertFalse(too_big["ok"])

    async def test_close_eof_half_close(self) -> None:
        _svc, _rt, ws_id, _if, handlers = _setup()
        await handlers["workspace:stream_start"](
            {
                "connection_id": "e1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["cat"],
            }
        )
        result = await handlers["workspace:stream_close"](
            {
                "connection_id": "e1",
                "workspace_id": str(ws_id),
                "eof": True,
            }
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("eof"))

    async def test_output_uses_ack_call_and_closed_event(self) -> None:
        _svc, _rt, ws_id, interface, handlers = _setup()
        await handlers["workspace:stream_start"](
            {
                "connection_id": "o1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["cat"],
            }
        )
        # Pump task runs in background; give it a chance to hit EOF and
        # emit workspace:stream_closed.
        pump = interface._running_tasks.get("stream:o1")
        self.assertIsNotNone(pump)
        await asyncio.wait_for(pump, timeout=5)
        calls = [
            c.args[0]
            for c in interface._sio.emit.await_args_list
            if c.args[0] == "workspace:stream_closed"
        ]
        self.assertTrue(calls)
        closed_payload = interface._sio.emit.await_args_list[-1].args[1]
        self.assertEqual(closed_payload["connection_id"], "o1")

    async def test_disconnect_closes_streams(self) -> None:
        _svc, _rt, ws_id, _interface, handlers = _setup()
        await handlers["workspace:stream_start"](
            {
                "connection_id": "d1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["sleep", "5"],
            }
        )
        self.assertIn("d1", _svc._streams)
        await handlers["disconnect"]()
        self.assertNotIn("d1", _svc._streams)

    async def test_cancelled_pump_emits_plain_close_no_error(self) -> None:
        """Cancelled pump task cleans up silently (no spurious error)."""
        _svc, _rt, ws_id, interface, handlers = _setup()
        await handlers["workspace:stream_start"](
            {
                "connection_id": "c1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["sleep", "30"],
            }
        )
        pump = interface._running_tasks.get("stream:c1")
        self.assertIsNotNone(pump)
        assert pump is not None
        # Full close path via the real handler: it pops + cancels the
        # pump, then closes the session. The close handler itself
        # returns ok (it never emits stream_closed — the pump does that
        # on natural EOF or genuine errors only). A cancelled pump must
        # NOT emit "stream output ACK timed out".
        result = await handlers["workspace:stream_close"](
            {"connection_id": "c1", "workspace_id": str(ws_id)}
        )
        self.assertTrue(result.get("ok"))
        errors = [
            c.args[1]
            for c in interface._sio.emit.await_args_list
            if c.args[0] == "workspace:stream_closed"
            and isinstance(c.args[1], dict)
            and "error" in c.args[1]
        ]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()


class StreamPumpConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stderr_only_process_does_not_block(self) -> None:
        """A stderr-only process must still deliver output (no stdout block)."""
        import base64 as _b64

        from src.service import STREAM_CHUNK_SIZE  # noqa: F401

        _svc, _rt, ws_id, interface, handlers = _setup()

        # Fake runtime: stdout is EOF immediately, stderr has data.
        async def _read(handle, stream="stdout", size=65536):
            if stream == "stderr":
                pending = handle.metadata.setdefault("stderr_pending", [b"err-line"])
                if pending:
                    return pending.pop(0)
                return b""
            return b""

        _rt.process_read = _read  # type: ignore[method-assign]
        await handlers["workspace:stream_start"](
            {
                "connection_id": "so1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["noisy-stderr"],
            }
        )
        pump = interface._running_tasks.get("stream:so1")
        self.assertIsNotNone(pump)
        await asyncio.wait_for(pump, timeout=10)
        # Output chunks go through sio.call (ACK/backpressure); the final
        # close notice goes through sio.emit.
        stderr_calls = [
            c
            for c in interface._sio.call.await_args_list
            if c.args[0] == "workspace:stream_output"
            and c.args[1].get("stream") == "stderr"
        ]
        self.assertTrue(stderr_calls, "stderr-only output was never emitted")
        payload = stderr_calls[0].args[1]
        self.assertEqual(_b64.b64decode(payload["data"]), b"err-line")

    async def test_eof_half_close_keeps_pump_alive(self) -> None:
        """workspace:stream_close eof=True must NOT cancel the output pump."""
        _svc, _rt, ws_id, interface, handlers = _setup()
        await handlers["workspace:stream_start"](
            {
                "connection_id": "eo1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["cat"],
            }
        )
        pump_before = interface._running_tasks.get("stream:eo1")
        self.assertIsNotNone(pump_before)
        result = await handlers["workspace:stream_close"](
            {
                "connection_id": "eo1",
                "workspace_id": str(ws_id),
                "eof": True,
            }
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("eof"))
        # Pump still registered and not done: full close cancels, EOF doesn't.
        pump_after = interface._running_tasks.get("stream:eo1")
        self.assertIs(pump_before, pump_after)
        self.assertFalse(pump_after.done())
        # Session still live (only stdin was half-closed).
        self.assertIn("eo1", _svc._streams)
        pump_after.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await pump_after

    async def test_natural_eof_removes_session_no_double_cancel(self) -> None:
        """Natural EOF reads exit code then removes/closes the session."""
        _svc, _rt, ws_id, interface, handlers = _setup()
        await handlers["workspace:stream_start"](
            {
                "connection_id": "ne1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["true"],
            }
        )
        pump = interface._running_tasks.get("stream:ne1")
        self.assertIsNotNone(pump)
        await asyncio.wait_for(pump, timeout=10)
        # Session removed and closed exactly once; task key popped.
        self.assertNotIn("ne1", _svc._streams)
        self.assertNotIn("stream:ne1", interface._running_tasks)
        closed_payloads = [
            c.args[1]
            for c in interface._sio.emit.await_args_list
            if c.args[0] == "workspace:stream_closed"
        ]
        self.assertTrue(closed_payloads)
        self.assertEqual(closed_payloads[-1]["connection_id"], "ne1")

    async def test_output_nack_closes_runner_side(self) -> None:
        """Backend {ok:false} ACK must fail the pump and close the session."""
        _svc, _rt, ws_id, interface, handlers = _setup()

        async def _read(handle, stream="stdout", size=65536):
            pending = handle.metadata.setdefault("nack_pending", [b"data"])
            if pending and stream == "stdout":
                return pending.pop(0)
            return b""

        _rt.process_read = _read  # type: ignore[method-assign]
        interface._sio.call = AsyncMock(return_value={"ok": False})
        await handlers["workspace:stream_start"](
            {
                "connection_id": "na1",
                "workspace_id": str(ws_id),
                "kind": "process",
                "command": ["yes"],
            }
        )
        pump = interface._running_tasks.get("stream:na1")
        self.assertIsNotNone(pump)
        await asyncio.wait_for(pump, timeout=10)
        self.assertNotIn("na1", _svc._streams)
        errors = [
            c.args[1]
            for c in interface._sio.emit.await_args_list
            if c.args[0] == "workspace:stream_closed"
            and c.args[1].get("error")
        ]
        self.assertTrue(errors)

    async def test_qemu_stderr_only_and_kill_fake(self) -> None:
        """QEMU split streams: stderr-only read works; close kills once."""
        from unittest.mock import MagicMock

        from src.runtime.base import ProcessHandle as _PH
        from src.runtime.qemu_runtime import QemuRuntime

        runtime = object.__new__(QemuRuntime)

        class FakeReader:
            def __init__(self, chunks: list[bytes]) -> None:
                self._chunks = list(chunks)

            def at_eof(self) -> bool:
                return not self._chunks

            async def read(self, _n: int) -> bytes:
                if self._chunks:
                    return self._chunks.pop(0)
                return b""

        fake_process = MagicMock()
        fake_process.stdout = FakeReader([])
        fake_process.stderr = FakeReader([b"only-err"])
        fake_process.stdin = MagicMock()
        fake_process.wait_closed = AsyncMock()
        fake_process.close = MagicMock()
        fake_process.exit_status = 3
        runtime._kill_stream_tree = AsyncMock()  # type: ignore[method-assign]
        handle = _PH(instance_id="inst-9", handle=fake_process)
        handle.metadata.update(
            {"pidfile": "/tmp/p-kill", "pending_stdout": b"",
             "pending_stderr": b""}
        )
        # stderr-only: stdout EOF immediately, stderr delivers.
        self.assertEqual(await runtime.process_read(handle, "stdout"), b"")
        self.assertEqual(await runtime.process_read(handle, "stderr"), b"only-err")
        # Close: single wait-then-kill-then-close, no double wait hang.
        await runtime.process_close(handle)
        runtime._kill_stream_tree.assert_awaited_once_with(
            "inst-9", "/tmp/p-kill"
        )
        fake_process.close.assert_called_once()
        self.assertTrue(handle.closed)
        # Idempotent second close: no second kill.
        await runtime.process_close(handle)
        runtime._kill_stream_tree.assert_awaited_once()
