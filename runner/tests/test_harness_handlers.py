"""Tests for harness workspace-access handlers (runner side, fakes only)."""

from __future__ import annotations

import asyncio
import base64
import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface


class _FakeService:
    """Minimal fake WorkspaceService for harness handler tests."""

    def __init__(self) -> None:
        self.supported_runtimes = ["docker"]
        self.sync_from_runtime = AsyncMock()
        self.recover_desktop_sessions_from_runtime = AsyncMock()
        self.run_health_check_loop = AsyncMock()
        self.get_workspace_heartbeat_statuses = AsyncMock(return_value=[])
        self.exec_calls: list[dict] = []
        self.stream_payloads: list[tuple[str, object, object, object]] = []
        self.list_calls: list[tuple[str, str]] = []
        self.find_calls: list[tuple[object, str, object]] = []
        self.read_calls: list[tuple[str, str, object]] = []
        self.write_calls: list[tuple[str, str, str, int]] = []
        self.stat_calls: list[tuple[str, str]] = []
        self.desktop_action_calls: list[tuple[object, str, object]] = []
        self.stream_mode: str = "ok"
        self.wait_result = (0, "out", "err")
        self._desktop_sessions: dict = {}

    async def exec_harness_command(
        self, workspace_id, command, workdir="/workspace", env=None
    ):
        self.exec_calls.append(
            {
                "workspace_id": workspace_id,
                "command": command,
                "workdir": workdir,
                "env": env,
            }
        )
        if workdir != "/workspace" and "evil" in str(workdir):
            raise ValueError("Path must be under /workspace")
        return self.wait_result

    async def exec_harness_command_stream(
        self, workspace_id, command, workdir="/workspace", env=None
    ):
        self.stream_payloads.append((workspace_id, command, workdir, env))
        if self.stream_mode == "timeout":
            await asyncio.sleep(5)
            yield ("exit", "0")  # pragma: no cover
        elif self.stream_mode == "error":
            raise RuntimeError("boom")
        else:
            yield ("stdout", "hello")
            yield ("stderr", "oops")
            yield ("exit", "3")

    async def read_file(self, workspace_id, path, max_size=None):
        self.read_calls.append((workspace_id, path, max_size))
        if ".." in str(path):
            raise ValueError("Path must be under /workspace")
        return {
            "content": base64.b64encode(b"hi").decode(),
            "size": 2,
            "truncated": False,
            "mime_type": "text/plain",
        }

    async def write_file_content(
        self, workspace_id, path, content_b64, mode=0o644
    ):
        self.write_calls.append((workspace_id, path, content_b64, mode))
        if ".." in str(path):
            raise ValueError("Path must be under /workspace")

    async def list_files(self, workspace_id, path):
        self.list_calls.append((workspace_id, path))
        if ".." in str(path):
            raise ValueError("Path must be under /workspace")
        return [
            {
                "name": "a.txt",
                "path": "/workspace/a.txt",
                "type": "file",
                "size": 2,
            }
        ]

    async def find_files(self, workspace_id, query="", limit=50):
        self.find_calls.append((workspace_id, query, limit))
        if ".." in str(query):
            raise ValueError("Invalid find query")
        return {
            "paths": [{"name": "a.txt", "path": "/workspace/a.txt"}],
            "truncated": False,
        }

    async def stat_path(self, workspace_id, path):
        self.stat_calls.append((workspace_id, path))
        if ".." in str(path):
            raise ValueError("Path must be under /workspace")
        return {
            "path": "/workspace/a.txt",
            "is_dir": False,
            "size": 2,
            "mime_type": "text/plain",
        }

    async def desktop_action(self, workspace_id, action, args=None):
        self.desktop_action_calls.append((workspace_id, action, args))
        if action == "ensure":
            return {"ok": True, "display": ":1", "port": 6901}
        if action == "hold":
            # Hold installs a *new* session object (fresh Xvnc incarnation).
            # Tests asserting the idempotent hold path pre-seed the sentinel
            # and override desktop_action to skip this replacement.
            from unittest.mock import MagicMock

            fresh = MagicMock()
            fresh.port = 6901
            fresh.viewer_held = False
            fresh.computeruse_run_ids = {"run-1"}
            fresh.generation = getattr(
                self._desktop_sessions.get(workspace_id), "generation", 0
            ) + 1
            self._desktop_sessions[workspace_id] = fresh
            return {
                "ok": True,
                "display": ":1",
                "port": 6901,
                "viewer": False,
                "computer_use": True,
            }
        if action == "release":
            return {
                "ok": True,
                "stopped": False,
                "process_alive": True,
                "viewer_held": False,
                "computer_use_active": True,
            }
        if action == "screenshot" and args and args.get("fail"):
            raise RuntimeError("Desktop session is not active")
        if action == "record_start" and args and args.get("path") == "/etc/passwd":
            raise ValueError("Path must be under /workspace")
        return {"ok": True, "action": action}

    def get_desktop_state_payload(self, workspace_id):
        return {
            "workspace_id": str(workspace_id),
            "port": 6901,
            "container_ip": "172.22.0.2",
            "network_name": f"opencuria-ws-{workspace_id}",
            "viewer": False,
            "computer_use": True,
            "generation": 1,
        }

    def get_desktop_session(self, workspace_id):
        # Sessions as plain sentinels: identity comparison detects restarts.
        return self._desktop_sessions.get(workspace_id)

    async def start_desktop(self, workspace_id, width=None, height=None):
        if workspace_id in self._desktop_sessions:
            # Idempotent path: reuse the pre-seeded session object untouched
            # so tests can pin "same object → no tunnel close".
            return self._desktop_sessions[workspace_id]
        from unittest.mock import MagicMock

        session = MagicMock()
        session.port = 6901
        session.viewer_held = True
        session.computeruse_run_ids = set()
        session.generation = 1
        # Fresh start installs a new object.
        self._desktop_sessions[workspace_id] = session
        return session

    async def stop_desktop(self, workspace_id):
        from unittest.mock import MagicMock

        return MagicMock(
            stopped=False,
            process_alive=True,
            viewer_held=False,
            computer_use_active=True,
        )

    def get_desktop_container_ip(self, workspace_id):
        return "172.22.0.2"

    def get_desktop_network_name(self, workspace_id):
        return f"opencuria-ws-{workspace_id}"


def _interface(service: _FakeService) -> WebSocketInterface:
    interface = WebSocketInterface(service, RunnerSettings())
    interface._sio.emit = AsyncMock()
    return interface


def _payload(workspace_id: uuid.UUID, request_id: str, **extra) -> dict:
    return {
        "workspace_id": str(workspace_id),
        "request_id": request_id,
        **extra,
    }


class HarnessExecWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_wait_returns_separated_stdout_stderr(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:exec_wait"]
        await handler(
            _payload(
                workspace_id,
                "req-1",
                command=["echo", "hi"],
                workdir="/workspace",
                env={},
                timeout=10,
            )
        )
        interface._sio.emit.assert_awaited_with(
            "harness:exec_wait_result",
            {
                "workspace_id": str(workspace_id),
                "request_id": "req-1",
                "exit_code": 0,
                "stdout": "out",
                "stderr": "err",
            },
        )

    async def test_exec_wait_timeout_reports_error(self) -> None:
        service = _FakeService()

        async def _slow(*args, **kwargs):
            await asyncio.sleep(5)
            return (0, "", "")  # pragma: no cover

        service.exec_harness_command = _slow  # type: ignore[assignment]
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:exec_wait"]
        await handler(
            _payload(
                workspace_id,
                "req-t",
                command=["sleep", "5"],
                workdir="/workspace",
                env={},
                timeout=0.02,
            )
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:exec_wait_result")
        self.assertIn("timed out", payload["error"])


class HarnessExecStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_stream_emits_chunks_and_done(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:exec_stream"]
        await handler(
            _payload(
                workspace_id,
                "req-s",
                command=["ls"],
                workdir="/workspace",
                env={},
            )
        )
        task = interface._running_tasks["harness:req-s"]
        await task
        emitted = [call.args for call in interface._sio.emit.await_args_list]
        self.assertIn(
            (
                "harness:exec_chunk",
                {
                    "workspace_id": str(workspace_id),
                    "request_id": "req-s",
                    "stream": "stdout",
                    "data": "hello",
                },
            ),
            emitted,
        )
        self.assertIn(
            (
                "harness:exec_done",
                {
                    "workspace_id": str(workspace_id),
                    "request_id": "req-s",
                    "exit_code": 3,
                },
            ),
            emitted,
        )

    async def test_exec_stream_error_reports_done_error(self) -> None:
        service = _FakeService()
        service.stream_mode = "error"
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:exec_stream"]
        await handler(
            _payload(
                workspace_id, "req-e", command=["x"], workdir="/workspace"
            )
        )
        task = interface._running_tasks["harness:req-e"]
        await task
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:exec_done")
        self.assertEqual(payload["error"], "boom")

    async def test_exec_stream_cancel_stops_task(self) -> None:
        service = _FakeService()
        service.stream_mode = "timeout"
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:exec_stream"]
        await handler(
            _payload(
                workspace_id, "req-c", command=["sleep"], workdir="/workspace"
            )
        )
        cancel = interface._sio.handlers["/"]["harness:cancel"]
        await cancel({"request_id": "req-c"})
        task = interface._running_tasks.get("harness:req-c")
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertNotIn("harness:req-c", interface._running_tasks)


class HarnessFileHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_write_list_stat_ok(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()

        await interface._sio.handlers["/"]["harness:read_file"](
            _payload(workspace_id, "r1", path="/workspace/a.txt")
        )
        await interface._sio.handlers["/"]["harness:write_file"](
            _payload(
                workspace_id,
                "w1",
                path="/workspace/a.txt",
                content=base64.b64encode(b"hi").decode(),
                mode=0o644,
            )
        )
        await interface._sio.handlers["/"]["harness:list"](
            _payload(workspace_id, "l1", path="/workspace")
        )
        await interface._sio.handlers["/"]["harness:stat"](
            _payload(workspace_id, "s1", path="/workspace/a.txt")
        )

        emitted = {
            call.args[0]: call.args[1]
            for call in interface._sio.emit.await_args_list
        }
        self.assertEqual(
            emitted["harness:read_file_result"]["mime_type"], "text/plain"
        )
        self.assertTrue(emitted["harness:write_file_result"]["ok"])
        self.assertEqual(
            emitted["harness:list_result"]["entries"][0]["name"], "a.txt"
        )
        self.assertFalse(emitted["harness:stat_result"]["is_dir"])

    async def test_traversal_reports_error(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        await interface._sio.handlers["/"]["harness:read_file"](
            _payload(workspace_id, "r-evil", path="/workspace/../etc/passwd")
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:read_file_result")
        self.assertIn("/workspace", payload["error"])

    async def test_files_find_emits_capped_paths(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        await interface._sio.handlers["/"]["files:find"](
            _payload(workspace_id, "f1", query="a.txt", limit=50)
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "files:find_result")
        self.assertEqual(payload["paths"][0]["path"], "/workspace/a.txt")
        self.assertFalse(payload["truncated"])
        self.assertEqual(service.find_calls[0][1], "a.txt")

    async def test_files_find_invalid_query_reports_error(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        await interface._sio.handlers["/"]["files:find"](
            _payload(workspace_id, "f-evil", query="..")
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "files:find_result")
        self.assertEqual(payload["paths"], [])
        self.assertIn("Invalid find query", payload["error"])


class HarnessDesktopActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_action_emits_result(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        await handler(
            _payload(
                workspace_id,
                "d1",
                action="ensure",
                args={},
            )
        )
        interface._sio.emit.assert_any_await(
            "harness:desktop_action_result",
            {
                "workspace_id": str(workspace_id),
                "request_id": "d1",
                "ok": True,
                "display": ":1",
                "port": 6901,
            },
        )
        self.assertEqual(
            service.desktop_action_calls,
            [(workspace_id, "ensure", {})],
        )

    async def test_desktop_action_error_reports_error(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        await handler(
            _payload(
                workspace_id,
                "d2",
                action="screenshot",
                args={"fail": True},
            )
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:desktop_action_result")
        self.assertEqual(payload["error"], "Desktop session is not active")

    async def test_hold_emits_process_before_action_result(self) -> None:
        """Backend must cache proxy state before the child shows a viewer."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        await handler(
            _payload(
                workspace_id,
                "d-hold",
                action="hold",
                args={"kind": "computeruse", "run_id": "run-1"},
            )
        )
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:process", events)
        self.assertIn("harness:desktop_action_result", events)
        self.assertLess(
            events.index("desktop:process"),
            events.index("harness:desktop_action_result"),
        )
        process_payload = next(
            call.args[1]
            for call in interface._sio.emit.await_args_list
            if call.args[0] == "desktop:process"
        )
        self.assertEqual(process_payload["container_ip"], "172.22.0.2")
        self.assertEqual(
            process_payload["network_name"], f"opencuria-ws-{workspace_id}"
        )

    async def test_idempotent_hold_keeps_tunnels(self) -> None:
        """Same session object (no restart) must not close healthy tunnels."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        sentinel = object()
        service._desktop_sessions[workspace_id] = sentinel

        async def _idempotent_hold(ws_id, action, args=None):
            service.desktop_action_calls.append((ws_id, action, args))
            return {
                "ok": True,
                "display": ":1",
                "port": 6901,
                "viewer": False,
                "computer_use": True,
            }

        service.desktop_action = _idempotent_hold  # type: ignore[method-assign]
        closed: list = []

        async def _fake_close(ws_id):
            closed.append(ws_id)

        interface._close_desktop_proxy_tunnels_for_workspace = _fake_close  # type: ignore[assignment]
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        await handler(
            _payload(
                workspace_id,
                "d-hold-idem",
                action="hold",
                args={"kind": "computeruse", "run_id": "run-1"},
            )
        )
        self.assertEqual(closed, [])
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:process", events)
        self.assertIn("harness:desktop_action_result", events)
        self.assertLess(
            events.index("desktop:process"),
            events.index("harness:desktop_action_result"),
        )

    async def test_hold_restart_closes_tunnels_before_process(self) -> None:
        """Fresh Xvnc incarnation closes stale tunnels before desktop:process."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        other_id = uuid.uuid4()
        sentinel = object()
        service._desktop_sessions[workspace_id] = sentinel
        close_order: list = []
        closed: list = []
        orig_emit = interface._sio.emit

        async def _fake_close(ws_id):
            closed.append(ws_id)
            close_order.append("close")

        interface._close_desktop_proxy_tunnels_for_workspace = _fake_close  # type: ignore[assignment]

        async def _spy_emit(event, payload=None, *args, **kwargs):
            if event == "desktop:process":
                close_order.append("process")
            return await orig_emit(event, payload, *args, **kwargs)

        interface._sio.emit = _spy_emit  # type: ignore[method-assign]
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        await handler(
            _payload(
                workspace_id,
                "d-hold-restart",
                action="hold",
                args={"kind": "computeruse", "run_id": "run-1"},
            )
        )
        self.assertEqual(closed, [workspace_id])
        # The default fake replaces the session object, so a restart is
        # detected and the close precedes the lifecycle event.
        self.assertLess(
            close_order.index("close"),
            close_order.index("process"),
        )
        self.assertIsNot(service._desktop_sessions[workspace_id], sentinel)
        # Other workspaces are untouched: scoping is per-workspace.
        self.assertNotIn(other_id, closed)

    async def test_release_kept_emits_process_release_stopped_closes_tunnels(
        self,
    ) -> None:
        """Kept releases announce process; final releases stop + close tunnels."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        await handler(
            _payload(
                workspace_id,
                "d-kept",
                action="release",
                args={"kind": "computeruse", "run_id": "run-1"},
            )
        )
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:process", events)
        self.assertNotIn("desktop:stopped", events)

        class StoppingService(_FakeService):
            async def desktop_action(self, workspace_id, action, args=None):
                self.desktop_action_calls.append((workspace_id, action, args))
                return {
                    "ok": True,
                    "stopped": True,
                    "process_alive": False,
                    "viewer_held": False,
                    "computer_use_active": False,
                }

        service2 = StoppingService()
        interface2 = _interface(service2)
        interface2._desktop_proxy_tunnels["t1"] = object()
        closed: list = []

        async def _fake_close(ws_id):
            closed.append(ws_id)

        interface2._close_desktop_proxy_tunnels_for_workspace = _fake_close  # type: ignore[assignment]
        await interface2._sio.handlers["/"]["harness:desktop_action"](
            _payload(
                workspace_id,
                "d-stop",
                action="release",
                args={"kind": "computeruse", "run_id": "run-1"},
            )
        )
        events2 = [
            call.args[0] for call in interface2._sio.emit.await_args_list
        ]
        self.assertIn("desktop:stopped", events2)
        # Lifecycle stop is announced before the action result so the
        # backend clears proxy routing before the child tears down its
        # mini viewer.
        self.assertLess(
            events2.index("desktop:stopped"),
            events2.index("harness:desktop_action_result"),
        )
        self.assertEqual(closed, [workspace_id])

    async def test_screenshot_emits_no_lifecycle_event(self) -> None:
        """Normal I/O actions must not announce desktop lifecycle events."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        service.desktop_action_calls.clear()
        await handler(
            _payload(
                workspace_id,
                "d-shot",
                action="screenshot",
                args={},
            )
        )
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("harness:desktop_action_result", events)
        self.assertNotIn("desktop:process", events)
        self.assertNotIn("desktop:stopped", events)

    async def test_start_desktop_does_not_close_healthy_tunnels(self) -> None:
        """Idempotent viewer acquires must keep concurrent viewers attached."""
        from unittest.mock import MagicMock

        service = _FakeService()
        interface = _interface(service)
        interface._desktop_proxy_tunnels["t1"] = object()
        closed: list = []

        async def _fake_close(ws_id):
            closed.append(ws_id)

        interface._close_desktop_proxy_tunnels_for_workspace = _fake_close  # type: ignore[assignment]
        workspace_id = uuid.uuid4()
        session = MagicMock()
        session.port = 6901
        session.viewer_held = True
        session.computeruse_run_ids = set()
        session.generation = 1
        service._desktop_sessions[workspace_id] = session
        # Idempotent path: start_desktop reuses the pre-seeded session, so
        # before/after identity matches and no tunnel is closed.
        await interface._sio.handlers["/"]["task:start_desktop"](
            {"task_id": "task-1", "workspace_id": str(workspace_id)}
        )
        self.assertEqual(closed, [])
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:started", events)

    async def test_start_desktop_restart_closes_only_target_workspace(
        self,
    ) -> None:
        """Stale restart closes target tunnels before desktop:started."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        other_id = uuid.uuid4()
        sentinel = object()
        service._desktop_sessions[workspace_id] = sentinel
        service._desktop_sessions[other_id] = object()
        close_order: list = []
        closed: list = []
        orig_emit = interface._sio.emit

        async def _fake_close(ws_id):
            closed.append(ws_id)
            close_order.append("close")

        interface._close_desktop_proxy_tunnels_for_workspace = _fake_close  # type: ignore[assignment]

        async def _spy_emit(event, payload=None, *args, **kwargs):
            if event == "desktop:started":
                close_order.append("started")
            return await orig_emit(event, payload, *args, **kwargs)

        interface._sio.emit = _spy_emit  # type: ignore[method-assign]

        async def _restarting_start(ws_id, width=None, height=None):
            from unittest.mock import MagicMock

            fresh = MagicMock()
            fresh.port = 6901
            fresh.viewer_held = True
            fresh.computeruse_run_ids = set()
            fresh.generation = 2
            service._desktop_sessions[ws_id] = fresh
            return fresh

        service.start_desktop = _restarting_start  # type: ignore[method-assign]
        await interface._sio.handlers["/"]["task:start_desktop"](
            {"task_id": "task-2", "workspace_id": str(workspace_id)}
        )
        self.assertEqual(closed, [workspace_id])
        self.assertLess(
            close_order.index("close"),
            close_order.index("started"),
        )
        self.assertIsNot(service._desktop_sessions[workspace_id], sentinel)

    async def test_stop_desktop_kept_emits_viewer_released(self) -> None:
        """Viewer release with computer-use hold keeps the process cached."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        await interface._sio.handlers["/"]["task:stop_desktop"](
            {"task_id": "task-2", "workspace_id": str(workspace_id)}
        )
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:viewer_released", events)
        self.assertNotIn("desktop:stopped", events)

    async def test_desktop_action_registers_cancellable_task(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        task = asyncio.create_task(
            handler(
                _payload(
                    workspace_id,
                    "d-reg",
                    action="ensure",
                    args={},
                )
            )
        )
        await asyncio.sleep(0)
        self.assertIn("harness:d-reg", interface._running_tasks)
        await task
        self.assertNotIn("harness:d-reg", interface._running_tasks)

    async def test_desktop_execute_cancelled_by_harness_cancel(self) -> None:
        started = asyncio.Event()

        class BlockingService(_FakeService):
            async def desktop_action(self, workspace_id, action, args=None):
                self.desktop_action_calls.append((workspace_id, action, args))
                started.set()
                await asyncio.sleep(30)
                return {"ok": True}  # pragma: no cover

        service = BlockingService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler_task = asyncio.create_task(
            interface._sio.handlers["/"]["harness:desktop_action"](
                _payload(
                    workspace_id,
                    "d-exec",
                    action="execute",
                    args={"code": "import time"},
                )
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        self.assertIn("harness:d-exec", interface._running_tasks)
        cancel = interface._sio.handlers["/"]["harness:cancel"]
        await cancel({"request_id": "d-exec"})
        self.assertNotIn("harness:d-exec", interface._running_tasks)
        await handler_task
        emitted_events = [
            call.args[0] for call in interface._sio.emit.await_args_list
        ]
        self.assertNotIn("harness:desktop_action_result", emitted_events)


class HarnessServiceSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_file_content_rejects_traversal(self) -> None:
        from src.service import WorkspaceService

        service = WorkspaceService(runtimes={}, settings=RunnerSettings())
        with self.assertRaises(ValueError):
            await service.write_file_content(
                uuid.uuid4(), "/etc/passwd", base64.b64encode(b"x").decode()
            )

    async def test_stat_path_rejects_traversal(self) -> None:
        from src.service import WorkspaceService

        service = WorkspaceService(runtimes={}, settings=RunnerSettings())
        with self.assertRaises(ValueError):
            await service.stat_path(uuid.uuid4(), "/workspace/../evil")

    def test_parse_exec_output_splits_streams(self) -> None:
        from src.service import WorkspaceService

        stdout_b64 = base64.b64encode(b"out").decode()
        stderr_b64 = base64.b64encode(b"err").decode()
        output = (
            f"OPENCURIA_STDOUT\n{stdout_b64}\n"
            f"OPENCURIA_STDERR\n{stderr_b64}\nEXIT:0"
        )
        stdout, stderr = WorkspaceService._parse_harness_exec_output(output)
        self.assertEqual((stdout, stderr), ("out", "err"))


class HarnessServiceQuoteInjectionTests(unittest.IsolatedAsyncioTestCase):
    """Quote-injection: a ``'`` in the path must not break shell quoting."""

    def _service(self) -> tuple:
        from src.models import WorkspaceInfo
        from src.service import WorkspaceService

        captured: list[list[str]] = []

        class FakeRuntime:
            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                if command[:2] == ["realpath", "-m"]:
                    return 0, command[2] + "\n"
                captured.append(command)
                if command[:2] == ["sh", "-c"]:
                    return 0, "missing"
                if command[:2] == ["test", "-d"]:
                    return 1, ""
                return 0, ""

        runtime = FakeRuntime()
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
        return service, captured, workspace_id

    async def test_read_file_quotes_single_quote_path(self) -> None:
        import shlex

        service, captured, workspace_id = self._service()
        evil = "/workspace/x'; touch /tmp/pwned; echo '"
        with self.assertRaises((RuntimeError, FileNotFoundError)):
            await service.read_file(workspace_id, evil)
        shell_cmd = next(c[2] for c in captured if c[:2] == ["sh", "-c"])
        self.assertIn(shlex.quote(evil), shell_cmd)
        self.assertNotIn("'x'; touch", shell_cmd.replace(shlex.quote(evil), ""))

    async def test_stat_path_quotes_single_quote_path(self) -> None:
        import shlex

        service, captured, workspace_id = self._service()
        evil = "/workspace/x'; touch /tmp/pwned; echo '"
        with self.assertRaises(FileNotFoundError):
            await service.stat_path(workspace_id, evil)
        shell_cmd = next(c[2] for c in captured if c[:2] == ["sh", "-c"])
        self.assertIn(shlex.quote(evil), shell_cmd)

    async def test_download_dir_quotes_single_quote_path(self) -> None:
        import shlex

        from src.models import WorkspaceInfo

        service, captured, workspace_id = self._service()

        class DirRuntime:
            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                if command[:2] == ["realpath", "-m"]:
                    return 0, command[2] + "\n"
                captured.append(command)
                if command[:2] == ["test", "-d"]:
                    return 0, ""
                return 0, "QUJD"

        service._runtimes = {"docker": DirRuntime()}
        info = service._cache[workspace_id]
        service._cache[workspace_id] = WorkspaceInfo(
            workspace_id=info.workspace_id,
            instance_id="instance-1",
            status="running",
            runtime_type="docker",
        )
        evil = "/workspace/x'; touch /tmp/pwned; echo '"
        result = await service.download_file(workspace_id, evil)
        self.assertTrue(result["content"])
        shell_cmd = next(c[2] for c in captured if c[:2] == ["sh", "-c"])
        self.assertIn(shlex.quote(evil.rsplit("/", 1)[0]), shell_cmd)


class HarnessServiceSymlinkEscapeTests(unittest.IsolatedAsyncioTestCase):
    """Symlink containment: realpath escapes fail closed with ValueError."""

    def _service(self, resolved: str):
        from src.models import WorkspaceInfo
        from src.service import WorkspaceService

        class FakeRuntime:
            def __init__(self) -> None:
                self.calls = 0

            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                self.calls += 1
                return 0, resolved + "\n"

        runtime = FakeRuntime()
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

    async def test_read_file_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc/shadow")
        with self.assertRaises(ValueError):
            await service.read_file(workspace_id, "/workspace/link")

    async def test_stat_path_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc/passwd")
        with self.assertRaises(ValueError):
            await service.stat_path(workspace_id, "/workspace/link")

    async def test_list_files_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc")
        with self.assertRaises(ValueError):
            await service.list_files(workspace_id, "/workspace/link")

    async def test_download_file_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc/shadow")
        with self.assertRaises(ValueError):
            await service.download_file(workspace_id, "/workspace/link")

    async def test_write_file_content_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc/cron.d/evil")
        with self.assertRaises(ValueError):
            await service.write_file_content(
                workspace_id,
                "/workspace/link",
                base64.b64encode(b"x").decode(),
            )

    async def test_normal_path_passes_realpath_check(self) -> None:
        service, runtime, workspace_id = self._service(
            "/workspace/sub/file.txt"
        )
        resolved = await service._realpath_under_workspace(
            service._get_runtime(workspace_id),
            "instance-1",
            "/workspace/sub/file.txt",
        )
        self.assertEqual(resolved, "/workspace/sub/file.txt")
        self.assertEqual(runtime.calls, 1)

    async def test_realpath_missing_falls_back_tolerant(self) -> None:
        from src.service import WorkspaceService

        class FailingRuntime:
            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                return 127, "realpath: command not found"

        runtime = FailingRuntime()
        service = WorkspaceService(
            runtimes={"docker": runtime}, settings=RunnerSettings()
        )
        resolved = await service._realpath_under_workspace(
            runtime, "instance-1", "/workspace/a.txt"
        )
        self.assertEqual(resolved, "/workspace/a.txt")


if __name__ == "__main__":
    unittest.main()


class HarnessExecWaitTrackedTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_wait_tracked_and_cancellable(self) -> None:
        service = _FakeService()

        async def _slow(*args, **kwargs):
            await asyncio.sleep(5)
            return (0, "", "")  # pragma: no cover

        service.exec_harness_command = _slow  # type: ignore[assignment]
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:exec_wait"]
        task = asyncio.create_task(
            handler(
                _payload(
                    workspace_id,
                    "req-w",
                    command=["sleep", "5"],
                    workdir="/workspace",
                    env={},
                    timeout=30,
                )
            )
        )
        await asyncio.sleep(0.05)
        self.assertIn("harness:req-w", interface._running_tasks)
        cancel = interface._sio.handlers["/"]["harness:cancel"]
        await cancel({"request_id": "req-w"})
        await task
        await asyncio.sleep(0)
        self.assertNotIn("harness:req-w", interface._running_tasks)
