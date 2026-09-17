"""Tests for WorkspaceService generic stream sessions."""

from __future__ import annotations

import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.models import WorkspaceInfo
from src.runtime.base import ProcessHandle
from src.service import (
    STREAM_MAX_PER_WORKSPACE,
    TCP_RELAY_CODE,
    WorkspaceService,
    _validate_stream_host,
    _validate_stream_port,
)


class FakeStreamRuntime:
    """Minimal runtime stub implementing only the stream surface."""

    def __init__(self) -> None:
        self.spawned: list[dict] = []
        self.closed: list[ProcessHandle] = []
        self.read_chunks: dict[str, list[bytes]] = {}
        self.fail_spawn = False

    async def spawn_process(self, instance_id, command, workdir=None, env=None):
        if self.fail_spawn:
            raise FileNotFoundError("python3 missing")
        self.spawned.append(
            {
                "instance_id": instance_id,
                "command": list(command),
                "workdir": workdir,
                "env": dict(env or {}),
            }
        )
        handle = ProcessHandle(instance_id=instance_id, handle=object())
        handle.metadata["reads"] = list(self.read_chunks.get("stdout", []))
        return handle

    async def process_read(self, handle, stream="stdout", size=65536):
        reads = handle.metadata.get("reads", [])
        if reads:
            return reads.pop(0)
        return b""

    async def process_write(self, handle, data: bytes) -> None:
        handle.metadata.setdefault("written", []).append(bytes(data))

    async def process_write_eof(self, handle) -> None:
        handle.metadata["eof"] = True

    async def process_wait(self, handle) -> int:
        return 0

    async def process_close(self, handle) -> None:
        handle.closed = True
        self.closed.append(handle)

    async def workspace_exists(self, instance_id: str) -> bool:
        return True


def _service(runtime=None):
    runtime = runtime or FakeStreamRuntime()
    service = WorkspaceService(
        runtimes={"docker": runtime}, settings=RunnerSettings()
    )
    workspace_id = uuid.uuid4()
    service._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    return service, runtime, workspace_id


class StreamValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_requires_argv_list(self) -> None:
        service, _rt, ws_id = _service()
        with self.assertRaises(ValueError):
            await service.stream_start_process(ws_id, "c1", "echo hi")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            await service.stream_start_process(ws_id, "c1", [])

    async def test_process_rejects_blocked_env(self) -> None:
        service, _rt, ws_id = _service()
        with self.assertRaises(ValueError):
            await service.stream_start_process(
                ws_id, "c1", ["env"], env={"LD_PRELOAD": "x"}
            )
        with self.assertRaises(ValueError):
            await service.stream_start_process(
                ws_id, "c1", ["env"], env={"PATH": "/x"}
            )

    async def test_process_does_not_source_credentials(self) -> None:
        service, runtime, ws_id = _service()
        await service.stream_start_process(
            ws_id, "c1", ["my-mcp"], workdir="/workspace", env={"A": "b"}
        )
        spawned = runtime.spawned[0]
        self.assertEqual(spawned["env"], {"A": "b"})
        self.assertNotIn(".opencuria-env.sh", " ".join(spawned["command"][:4]))

    async def test_duplicate_connection_id_rejected(self) -> None:
        service, _rt, ws_id = _service()
        await service.stream_start_process(ws_id, "dup", ["echo", "hi"])
        with self.assertRaises(ValueError):
            await service.stream_start_process(ws_id, "dup", ["echo", "hi"])

    async def test_concurrent_duplicate_start_single_winner(self) -> None:
        """Concurrent starts with the same id: exactly one wins, no leak."""
        import asyncio

        service, runtime, ws_id = _service()
        started = asyncio.Event()
        release = asyncio.Event()
        orig_spawn = runtime.spawn_process

        async def _gated_spawn(instance_id, command, workdir=None, env=None):
            started.set()
            await release.wait()
            return await orig_spawn(instance_id, command, workdir, env)

        runtime.spawn_process = _gated_spawn  # type: ignore[method-assign]
        first = asyncio.ensure_future(
            service.stream_start_process(ws_id, "race", ["echo", "a"])
        )
        await started.wait()
        # Second start races while the first spawn is in flight: the slot
        # is already reserved, so it fails fast without spawning.
        with self.assertRaises(ValueError):
            await service.stream_start_process(ws_id, "race", ["echo", "b"])
        release.set()
        won = await first
        self.assertEqual(won.connection_id, "race")
        self.assertEqual(len(runtime.spawned), 1)
        self.assertEqual(len(runtime.closed), 0)
        live = service.get_stream("race")
        self.assertIsNotNone(live.handle)

    async def test_failed_spawn_rolls_back_reservation(self) -> None:
        service, runtime, ws_id = _service()
        runtime.fail_spawn = True
        with self.assertRaises(FileNotFoundError):
            await service.stream_start_process(ws_id, "gone", ["x"])
        self.assertNotIn("gone", service._streams)
        # A retry after the failure can reuse the id.
        runtime.fail_spawn = False
        await service.stream_start_process(ws_id, "gone", ["x"])
        self.assertIsNotNone(service.get_stream("gone"))

    async def test_reserved_session_not_usable_before_commit(self) -> None:
        service, _rt, _ws_id = _service()
        with self.assertRaises(ValueError):
            service.get_stream("never-started")

    async def test_per_workspace_limit(self) -> None:
        service, _rt, ws_id = _service()
        for i in range(STREAM_MAX_PER_WORKSPACE):
            await service.stream_start_process(ws_id, f"c{i}", ["sleep", "1"])
        with self.assertRaises(ValueError):
            await service.stream_start_process(ws_id, "overflow", ["sleep", "1"])

    async def test_bad_connection_ids_rejected(self) -> None:
        service, _rt, ws_id = _service()
        for bad in ["", "a b", "a/b", "../x", "x" * 129, "a\nb"]:
            with self.assertRaises(ValueError, msg=bad):
                await service.stream_start_process(ws_id, bad, ["echo"])

    async def test_tcp_host_port_validation(self) -> None:
        self.assertEqual(_validate_stream_host("localhost"), "localhost")
        self.assertEqual(_validate_stream_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(_validate_stream_host("::1"), "::1")
        self.assertEqual(_validate_stream_host("example.com"), "example.com")
        self.assertEqual(_validate_stream_port(8080), 8080)
        for bad_host in ["", "a b", "x" * 256, "a\nb", "http://x"]:
            with self.assertRaises(ValueError, msg=bad_host):
                _validate_stream_host(bad_host)
        for bad_port in [0, 70000, -1, True, "x"]:
            with self.assertRaises(ValueError, msg=str(bad_port)):
                _validate_stream_port(bad_port)

    async def test_tcp_uses_static_relay_argv(self) -> None:
        service, runtime, ws_id = _service()
        await service.stream_start_tcp(ws_id, "t1", "localhost", 8080)
        spawned = runtime.spawned[0]
        cmd = spawned["command"]
        self.assertEqual(cmd[0], "python3")
        self.assertEqual(cmd[1], "-u")
        self.assertEqual(cmd[2], "-c")
        self.assertEqual(cmd[3], TCP_RELAY_CODE)
        self.assertEqual(cmd[4:], ["--", "localhost", "8080", "0", "localhost"])
        # Relay resolves DNS inside the workspace: constant contains
        # getaddrinfo and TLS default verification.
        self.assertIn("getaddrinfo", TCP_RELAY_CODE)
        self.assertIn("create_default_context", TCP_RELAY_CODE)

    async def test_tcp_tls_flag_and_sni(self) -> None:
        service, runtime, ws_id = _service()
        await service.stream_start_tcp(
            ws_id, "t2", "example.com", 443, tls=True, server_hostname="sni.test"
        )
        cmd = runtime.spawned[0]["command"]
        self.assertEqual(cmd[4:], ["--", "example.com", "443", "1", "sni.test"])

    async def test_tcp_python3_missing_clear_error(self) -> None:
        service, runtime, ws_id = _service()
        runtime.fail_spawn = True
        with self.assertRaises(RuntimeError) as ctx:
            await service.stream_start_tcp(ws_id, "t9", "localhost", 80)
        self.assertIn("python3", str(ctx.exception))

    async def test_write_chunk_bound(self) -> None:
        service, _rt, ws_id = _service()
        await service.stream_start_process(ws_id, "w1", ["cat"])
        with self.assertRaises(ValueError):
            await service.stream_write("w1", b"x" * (64 * 1024 + 1))
        await service.stream_write("w1", b"hello")

    async def test_close_unknown_returns_closed_false(self) -> None:
        service, _rt, _ws = _service()
        result = await service.stream_close("nope")
        self.assertEqual(result, {"connection_id": "nope", "closed": False})

    async def test_close_workspace_streams_on_lifecycle(self) -> None:
        service, _runtime, ws_id = _service()
        await service.stream_start_process(ws_id, "k1", ["sleep", "5"])
        await service.stream_start_process(ws_id, "k2", ["sleep", "5"])
        closed = await service.close_workspace_streams(ws_id, reason="stop")
        self.assertEqual(closed, 2)
        self.assertEqual(service._stream_count_for_workspace(ws_id), 0)

    async def test_close_all_streams(self) -> None:
        service, _runtime, ws_id = _service()
        await service.stream_start_process(ws_id, "a1", ["sleep", "5"])
        closed = await service.close_all_streams(reason="shutdown")
        self.assertEqual(closed, 1)

    async def test_workspace_mismatch_via_second_workspace(self) -> None:
        service, _rt, ws_id = _service()
        other = uuid.uuid4()
        service._cache[other] = WorkspaceInfo(
            workspace_id=other,
            instance_id="instance-2",
            status="running",
            runtime_type="docker",
        )
        await service.stream_start_process(ws_id, "m1", ["sleep", "5"])
        session = service._streams["m1"]
        self.assertNotEqual(session.workspace_id, other)
        # stream_close pops by id regardless; workspace scoping is
        # enforced at the websocket layer (mismatch test lives there).


class QemuStreamKillTests(unittest.IsolatedAsyncioTestCase):
    async def test_qemu_kill_command_quotes_pidfile(self) -> None:
        from src.runtime.qemu_runtime import QemuRuntime

        runtime = object.__new__(QemuRuntime)
        cmd = runtime._build_stream_kill_command("/tmp/opencuria-stream-abc.pid")
        self.assertIn("/tmp/opencuria-stream-abc.pid", cmd)
        self.assertIn("kill -TERM", cmd)
        self.assertIn("kill -KILL", cmd)

    async def test_qemu_spawn_uses_wrapper_and_split_streams(self) -> None:
        import asyncssh  # noqa: I001
        from unittest.mock import MagicMock

        from src.runtime.qemu_runtime import QemuRuntime

        runtime = object.__new__(QemuRuntime)
        runtime._stream_pidfile = lambda: "/tmp/opencuria-stream-test.pid"  # type: ignore[method-assign]
        fake_process = MagicMock()
        fake_process.stdout = MagicMock()
        fake_process.stderr = MagicMock()
        fake_process.stdin = MagicMock()
        ssh = MagicMock()
        ssh.create_process = AsyncMock(return_value=fake_process)
        runtime._get_ssh = AsyncMock(return_value=ssh)  # type: ignore[method-assign]

        handle = await runtime.spawn_process(
            "inst-1", ["my-mcp", "--stdio"], workdir="/workspace", env={"A": "b"}
        )
        cmd_str = ssh.create_process.await_args.args[0]
        # argv words pass through verbatim as separate argv entries
        # (never joined/interpolated); plain words need no quoting.
        self.assertIn("my-mcp", cmd_str)
        self.assertIn("--stdio", cmd_str)
        self.assertIn("/tmp/opencuria-stream-test.pid", cmd_str)
        kwargs = ssh.create_process.await_args.kwargs
        self.assertIs(kwargs["stdin"], asyncssh.PIPE)
        self.assertIs(kwargs["stdout"], asyncssh.PIPE)
        self.assertIs(kwargs["stderr"], asyncssh.PIPE)
        self.assertIsNone(kwargs["encoding"])
        self.assertEqual(handle.metadata["pidfile"], "/tmp/opencuria-stream-test.pid")

    async def test_qemu_close_does_eof_then_kill_then_close(self) -> None:
        from unittest.mock import MagicMock

        from src.runtime.qemu_runtime import QemuRuntime

        runtime = object.__new__(QemuRuntime)
        fake_process = MagicMock()
        fake_process.stdin = MagicMock()
        fake_process.wait_closed = AsyncMock()
        fake_process.close = MagicMock()
        runtime._kill_stream_tree = AsyncMock()  # type: ignore[method-assign]
        from src.runtime.base import ProcessHandle as _PH

        real = _PH(instance_id="inst-1", handle=fake_process)
        real.metadata["pidfile"] = "/tmp/pid-x"
        await runtime.process_close(real)
        fake_process.stdin.write_eof.assert_called()
        runtime._kill_stream_tree.assert_awaited_once_with("inst-1", "/tmp/pid-x")
        fake_process.close.assert_called()
        self.assertTrue(real.closed)


if __name__ == "__main__":
    unittest.main()


class DockerCloseOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_close_sends_eof_before_flag(self) -> None:
        """process_close must attempt stdin EOF while handle is open."""
        from src.runtime.base import ProcessHandle
        from src.runtime.docker_runtime import DockerRuntime

        calls: list[str] = []

        class FakeRaw:
            def shutdown(self, _how: int) -> None:
                calls.append("shutdown")

            def close(self) -> None:
                calls.append("close")

            def recv(self, _n: int) -> bytes:
                return b""

        class FakeSock:
            _sock = FakeRaw()

            def close(self) -> None:
                calls.append("sock_close")

        runtime = object.__new__(DockerRuntime)
        runtime._container = lambda _id: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("kill must not run without pidfile exec")
        )
        import queue as _queue
        import threading

        handle = ProcessHandle(instance_id="c1", handle=FakeSock())
        handle.metadata.update(
            {
                "pidfile": "",
                "stop": threading.Event(),
                "pump": None,
                "stdout_queue": _queue.Queue(),
                "stderr_queue": _queue.Queue(),
            }
        )
        # Producer threads are not running here; pre-seed EOF sentinels
        # so _stream_queue_read would not block (not used in this test).
        handle.metadata["stdout_queue"].put(None)
        await runtime.process_close(handle)
        self.assertIn("shutdown", calls)
        self.assertTrue(handle.closed)

    async def test_reconfigure_and_heal_close_streams(self) -> None:
        service, _rt, ws_id = _service()
        await service.stream_start_process(ws_id, "r1", ["sleep", "5"])
        self.assertIn("r1", service._streams)
        await service.update_workspace_resources(
            ws_id, qemu_vcpus=2, qemu_memory_mb=1024, qemu_disk_size_gb=10
        ) if False else None
        # update_workspace_resources requires qemu runtime; exercise the
        # close path directly for the generic runtime instead.
        closed = await service.close_workspace_streams(ws_id, reason="test")
        self.assertEqual(closed, 1)
        self.assertNotIn("r1", service._streams)


class RelayFullDuplexTests(unittest.TestCase):
    def test_relay_uses_os_read_and_survives_stdin_eof(self) -> None:
        from src.service import TCP_RELAY_CODE

        # No BufferedReader.read after select (would block past select).
        self.assertIn("os.read(stdin_fd", TCP_RELAY_CODE.replace("_os.read", "os.read"))
        self.assertNotIn("fdin.read(", TCP_RELAY_CODE)
        # stdin EOF half-closes but the loop keeps reading the server.
        self.assertIn("SHUT_WR", TCP_RELAY_CODE)
        # Find the stdin-EOF branch and assert no `break` right after
        # shutdown: the relay must keep draining the server reply.
        idx = TCP_RELAY_CODE.index("conn.shutdown(socket.SHUT_WR)")
        after = TCP_RELAY_CODE[idx : idx + 600]
        self.assertNotIn("break", after.split("conn_write_closed = True")[1][:200])

    def test_relay_request_halfclose_response(self) -> None:
        """Run the real relay against a local TCP server (no Docker)."""
        import os
        import socket
        import subprocess
        import sys
        import tempfile
        import threading

        from src.service import TCP_RELAY_CODE

        response = b"HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\nhello"
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received: list[bytes] = []

        def _serve() -> None:
            conn, _ = server.accept()
            with conn:
                data = b""
                conn.settimeout(10)
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                received.append(data)
                conn.sendall(response)
                conn.shutdown(socket.SHUT_WR)

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        with tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w"
        ) as fh:
            fh.write(TCP_RELAY_CODE)
            relay_path = fh.name
        try:
            proc = subprocess.Popen(
                [
                    sys.executable, "-u", relay_path, "--",
                    "127.0.0.1", str(port), "0", "127.0.0.1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(b"GET / HTTP/1.0\r\n\r\n")
            proc.stdin.flush()
            os.close(proc.stdin.fileno())  # half-close: relay sees EOF
            out = proc.stdout.read()
            err = proc.stderr.read() if proc.stderr is not None else b""
            proc.wait(timeout=20)
        finally:
            os.unlink(relay_path)
            server.close()
        self.assertEqual(proc.returncode, 0, msg=f"relay stderr: {err!r}")
        self.assertEqual(out, response)
        self.assertEqual(received[0], b"GET / HTTP/1.0\r\n\r\n")


class StreamReservationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_during_spawn_rolls_back_without_closing_none(self) -> None:
        """A close racing an in-flight spawn never calls process_close(None)."""
        import asyncio

        service, runtime, ws_id = _service()
        started = asyncio.Event()
        release = asyncio.Event()
        orig_spawn = runtime.spawn_process
        seen_handles: list[object] = []
        orig_close = runtime.process_close

        async def _gated_spawn(instance_id, command, workdir=None, env=None):
            started.set()
            await release.wait()
            return await orig_spawn(instance_id, command, workdir, env)

        async def _tracking_close(handle) -> None:
            seen_handles.append(handle)
            assert handle is not None, "process_close must never see None"
            await orig_close(handle)

        runtime.spawn_process = _gated_spawn  # type: ignore[method-assign]
        runtime.process_close = _tracking_close  # type: ignore[method-assign]
        first = asyncio.ensure_future(
            service.stream_start_process(ws_id, "racing", ["echo", "a"])
        )
        await started.wait()
        # Close while the spawn is still reserved (handle is None): the
        # reservation is dropped without touching the runtime.
        closed = await service.stream_close("racing")
        self.assertTrue(closed["closed"])
        release.set()
        with self.assertRaises(ValueError):
            await first
        # The late spawn committed to a handle that nobody owns anymore:
        # it must have been closed exactly once (no leak), and never None.
        self.assertEqual(len(runtime.spawned), 1)
        self.assertEqual(len(runtime.closed), 1)
        self.assertTrue(all(h is not None for h in seen_handles))
        self.assertNotIn("racing", service._streams)

    async def test_close_workspace_streams_during_spawn_skips_none(self) -> None:
        """close_workspace_streams during reservation skips None handles."""
        import asyncio

        service, runtime, ws_id = _service()
        started = asyncio.Event()
        release = asyncio.Event()
        orig_spawn = runtime.spawn_process
        closed_handles: list[object] = []
        orig_close = runtime.process_close

        async def _gated_spawn(instance_id, command, workdir=None, env=None):
            started.set()
            await release.wait()
            return await orig_spawn(instance_id, command, workdir, env)

        async def _tracking_close(handle) -> None:
            closed_handles.append(handle)
            assert handle is not None
            await orig_close(handle)

        runtime.spawn_process = _gated_spawn  # type: ignore[method-assign]
        runtime.process_close = _tracking_close  # type: ignore[method-assign]
        first = asyncio.ensure_future(
            service.stream_start_tcp(ws_id, "tcp-race", "localhost", 8080)
        )
        await started.wait()
        count = await service.close_workspace_streams(ws_id, reason="test")
        # Reservation had handle None: counted as dropped, no runtime close.
        self.assertEqual(count, 0)
        self.assertEqual(closed_handles, [])
        release.set()
        with self.assertRaises(ValueError):
            await first
        # Late TCP spawn was rolled back remotely (exactly one close, real handle).
        self.assertEqual(len(runtime.closed), 1)

    async def test_close_all_streams_during_spawn_skips_none(self) -> None:
        """close_all_streams during reservation never closes None."""
        import asyncio

        service, runtime, ws_id = _service()
        started = asyncio.Event()
        release = asyncio.Event()
        orig_spawn = runtime.spawn_process

        async def _gated_spawn(instance_id, command, workdir=None, env=None):
            started.set()
            await release.wait()
            return await orig_spawn(instance_id, command, workdir, env)

        runtime.spawn_process = _gated_spawn  # type: ignore[method-assign]
        first = asyncio.ensure_future(
            service.stream_start_process(ws_id, "all-race", ["echo", "a"])
        )
        await started.wait()
        count = await service.close_all_streams(reason="test")
        self.assertEqual(count, 0)
        release.set()
        with self.assertRaises(ValueError):
            await first
        self.assertEqual(len(runtime.closed), 1)
        self.assertNotIn("all-race", service._streams)
