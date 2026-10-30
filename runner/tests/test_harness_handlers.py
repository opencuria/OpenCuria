"""Tests for harness workspace-access handlers (runner side, fakes only)."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import tarfile
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface


class _FakeService:
    """Minimal fake WorkspaceService for harness handler tests."""

    def __init__(self) -> None:
        self.exec_calls: list[dict] = []
        self.list_calls: list[tuple[str, str]] = []
        self.find_calls: list[tuple[object, str, object]] = []
        self.read_calls: list[tuple[str, str, object]] = []
        self.write_calls: list[tuple[str, str, str, int]] = []
        self.file_access_calls: list[tuple[str, str, bool]] = []
        self.desktop_action_calls: list[tuple[object, str, object]] = []
        self.wait_result = (0, "out", "err")
        # Mirror the manager surfaces used by the real WebSocket handlers.
        self.harness = self._FakeHarness(self)
        self.files = self._FakeFiles(self)
        self.desktop = self._FakeDesktop(self)

    class _FakeHarness:
        def __init__(self, outer: _FakeService) -> None:
            self._outer = outer

        async def exec_harness_command(
            self, workspace_id, command, workdir="/workspace", env=None
        ):
            return await self._outer._exec_harness_command_impl(
                workspace_id, command, workdir=workdir, env=env
            )

    class _FakeFiles:
        def __init__(self, outer: _FakeService) -> None:
            self._outer = outer

        async def read_file(self, workspace_id, path, max_size=None, *, allow_external=False):
            return await self._outer._read_file_impl(
                workspace_id, path, max_size=max_size, allow_external=allow_external
            )

        async def write_file_content(
            self, workspace_id, path, content_b64, mode=0o644, *, allow_external=False
        ):
            return await self._outer._write_file_content_impl(
                workspace_id, path, content_b64, mode=mode, allow_external=allow_external
            )

        async def list_files(self, workspace_id, path, *, allow_external=False):
            return await self._outer._list_files_impl(workspace_id, path, allow_external=allow_external)

        async def find_files(self, workspace_id, query="", limit=50):
            return await self._outer._find_files_impl(
                workspace_id, query=query, limit=limit
            )

    class _FakeDesktop:
        def __init__(self, outer: _FakeService) -> None:
            self._outer = outer
            self._desktop_sessions: dict = {}
            self.epoch = "fake-desktop-epoch"

        async def desktop_action(self, workspace_id, action, args=None):
            return await self._outer._desktop_action_impl(
                workspace_id, action, args
            )

        def get_desktop_state_payload(self, workspace_id):
            return self._outer._get_desktop_state_payload_impl(workspace_id)

        def get_desktop_session(self, workspace_id):
            # Sessions as plain sentinels: identity comparison detects restarts.
            return self._desktop_sessions.get(workspace_id)

        def get_desktop_guest_ip(self, workspace_id):
            return "172.22.0.2"

        def get_desktop_network_name(self, workspace_id):
            return f"opencuria-ws-{workspace_id}"

    async def _desktop_action_impl(self, workspace_id, action, args=None):
        self.desktop_action_calls.append((workspace_id, action, args))
        if action == "binding":
            return {"ok": True, "display": ":1", "port": 6901}
        if action == "hold":
            # Hold installs a *new* session object (fresh Xvnc incarnation).
            # Tests asserting the idempotent hold path pre-seed the sentinel
            # and override desktop_action to skip this replacement.
            from unittest.mock import MagicMock

            fresh = MagicMock()
            fresh.port = 6901
            fresh.viewer_held = args.get("kind") == "viewer"
            fresh.computeruse_run_ids = (
                {args.get("run_id", "run-1")}
                if args.get("kind") == "computeruse"
                else set()
            )
            fresh.generation = getattr(
                self.desktop._desktop_sessions.get(workspace_id), "generation", 0
            ) + 1
            self.desktop._desktop_sessions[workspace_id] = fresh
            return {
                "ok": True,
                "display": ":1",
                "port": 6901,
                "viewer": fresh.viewer_held,
                "computer_use": bool(fresh.computeruse_run_ids),
                "generation": fresh.generation,
                "epoch": "fake-desktop-epoch",
            }
        if action == "release":
            session = self.desktop._desktop_sessions.get(workspace_id)
            generation = getattr(session, "generation", 1)
            if session is not None:
                if args.get("kind") == "viewer":
                    session.viewer_held = False
                elif args.get("kind") == "computeruse":
                    session.computeruse_run_ids.discard(args.get("run_id", "run-1"))
                process_alive = bool(session.viewer_held or session.computeruse_run_ids)
                if not process_alive:
                    self.desktop._desktop_sessions.pop(workspace_id, None)
                    session = None
            else:
                process_alive = False
            return {
                "ok": True,
                "stopped": not process_alive,
                "process_alive": process_alive,
                "viewer_held": bool(session and session.viewer_held),
                "computer_use_active": bool(session and session.computeruse_run_ids),
                "generation": generation,
                "epoch": "fake-desktop-epoch",
            }
        if action == "record_start" and args and args.get("fail"):
            raise RuntimeError("Desktop session is not active")
        if action == "record_start" and args and args.get("path") == "/etc/passwd":
            raise ValueError("Path must be under /workspace")
        return {"ok": True, "action": action}

    def _get_desktop_state_payload_impl(self, workspace_id):
        session = self.desktop._desktop_sessions.get(workspace_id)
        if session is None:
            return None
        return {
            "workspace_id": str(workspace_id),
            "port": 6901,
            "guest_ip": "172.22.0.2",
            "viewer": session.viewer_held,
            "computer_use": bool(session.computeruse_run_ids),
            "generation": session.generation,
            "epoch": "fake-desktop-epoch",
        }

    async def _exec_harness_command_impl(
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

    @staticmethod
    def _is_external_path(path) -> bool:
        normalized = os.path.normpath(str(path))
        return normalized != "/workspace" and not normalized.startswith(
            "/workspace/"
        )

    async def _read_file_impl(
        self, workspace_id, path, max_size=None, *, allow_external=False
    ):
        self.read_calls.append((workspace_id, path, max_size))
        self.file_access_calls.append(("read", path, allow_external))
        if self._is_external_path(path) and not allow_external:
            raise ValueError("Path must be under /workspace")
        return {
            "content": base64.b64encode(b"hi").decode(),
            "size": 2,
            "truncated": False,
            "mime_type": "text/plain",
        }

    async def _write_file_content_impl(
        self, workspace_id, path, content_b64, mode=0o644, *, allow_external=False
    ):
        self.write_calls.append((workspace_id, path, content_b64, mode))
        self.file_access_calls.append(("write", path, allow_external))
        if self._is_external_path(path) and not allow_external:
            raise ValueError("Path must be under /workspace")

    async def _list_files_impl(self, workspace_id, path, *, allow_external=False):
        self.list_calls.append((workspace_id, path))
        self.file_access_calls.append(("list", path, allow_external))
        if self._is_external_path(path) and not allow_external:
            raise ValueError("Path must be under /workspace")
        return [
            {
                "name": "a.txt",
                "path": "/workspace/a.txt",
                "type": "file",
                "size": 2,
            }
        ]

    async def _find_files_impl(self, workspace_id, query="", limit=50):
        self.find_calls.append((workspace_id, query, limit))
        if ".." in str(query):
            raise ValueError("Invalid find query")
        return {
            "paths": [{"name": "a.txt", "path": "/workspace/a.txt"}],
            "truncated": False,
        }


def _interface(service: _FakeService) -> WebSocketInterface:
    interface = WebSocketInterface(service, RunnerSettings(state_dir=tempfile.mkdtemp()))
    interface._sio.emit = AsyncMock()
    return interface


def _payload(workspace_id: uuid.UUID, request_id: str, **extra) -> dict:
    return {
        "workspace_id": str(workspace_id),
        "request_id": request_id,
        **extra,
    }


class HarnessExecWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_stream_handler_is_not_registered(self) -> None:
        interface = _interface(_FakeService())
        handlers = interface._sio.handlers["/"]

        self.assertNotIn("harness:exec_stream", handlers)
        self.assertIn("harness:exec_wait", handlers)

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

        service.harness.exec_harness_command = _slow  # type: ignore[assignment]
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


class HarnessFileHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_list_ok(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()

        await interface._sio.handlers["/"]["harness:read_file"](
            _payload(workspace_id, "r1", path="/workspace/a.txt")
        )
        await interface._sio.handlers["/"]["harness:list"](
            _payload(workspace_id, "l1", path="/workspace")
        )

        emitted = {
            call.args[0]: call.args[1]
            for call in interface._sio.emit.await_args_list
        }
        self.assertEqual(
            emitted["harness:read_file_result"]["mime_type"], "text/plain"
        )
        self.assertEqual(
            emitted["harness:list_result"]["entries"][0]["name"], "a.txt"
        )

    async def test_harness_file_rpcs_allow_external_paths(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handlers = interface._sio.handlers["/"]
        path = "/etc/runner-test.txt"

        await handlers["harness:read_file"](
            _payload(workspace_id, "r-external", path=path)
        )
        await handlers["harness:list"](
            _payload(workspace_id, "l-external", path="/etc")
        )

        emitted = {
            call.args[0]: call.args[1]
            for call in interface._sio.emit.await_args_list
        }
        self.assertEqual(
            emitted["harness:read_file_result"]["content"],
            base64.b64encode(b"hi").decode(),
        )
        self.assertEqual(
            emitted["harness:list_result"]["entries"][0]["name"], "a.txt"
        )
        self.assertEqual(
            service.file_access_calls,
            [
                ("read", path, True),
                ("list", "/etc", True),
            ],
        )

    async def test_harness_chunked_write_allows_external_path(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handlers = interface._sio.handlers["/"]
        path = "/etc/chunked.txt"
        encoded_content = base64.b64encode(b"first second").decode()
        chunks = [encoded_content[:8], encoded_content[8:]]

        await handlers["harness:write_file_start"](
            _payload(
                workspace_id,
                "w-chunked-external",
                path=path,
                total_chunks=len(chunks),
                mode=0o640,
            )
        )
        # Send out of order to verify the transfer reassembles by chunk index.
        for index in reversed(range(len(chunks))):
            await handlers["harness:write_file_chunk"](
                _payload(
                    workspace_id,
                    "w-chunked-external",
                    path=path,
                    index=index,
                    total_chunks=len(chunks),
                    content=chunks[index],
                )
            )
        await handlers["harness:write_file_finish"](
            _payload(
                workspace_id,
                "w-chunked-external",
                path=path,
                total_chunks=len(chunks),
            )
        )

        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:write_file_result")
        self.assertTrue(payload["ok"])
        self.assertEqual(
            service.write_calls,
            [
                (
                    workspace_id,
                    path,
                    base64.b64encode(b"first second").decode(),
                    0o640,
                )
            ],
        )
        self.assertEqual(service.file_access_calls, [("write", path, True)])
        await handlers["harness:write_file_finish"](
            _payload(workspace_id, "w-chunked-external", path=path)
        )
        self.assertEqual(
            interface._sio.emit.await_args.args[1]["error"], "unknown write transfer"
        )

    async def test_file_explorer_rejects_external_paths_by_default(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handlers = interface._sio.handlers["/"]

        await handlers["files:list"](
            _payload(workspace_id, "l-external", path="/etc")
        )
        await handlers["files:read"](
            _payload(workspace_id, "r-external", path="/etc/passwd")
        )

        emitted = {
            call.args[0]: call.args[1]
            for call in interface._sio.emit.await_args_list
        }
        self.assertEqual(emitted["files:list_result"]["entries"], [])
        self.assertIn(
            "Path must be under /workspace",
            emitted["files:list_result"]["error"],
        )
        self.assertEqual(emitted["files:content_result"]["content"], "")
        self.assertIn(
            "Path must be under /workspace",
            emitted["files:content_result"]["error"],
        )
        self.assertEqual(
            service.file_access_calls,
            [("list", "/etc", False), ("read", "/etc/passwd", False)],
        )

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
    @staticmethod
    def _viewer_lease(workspace_id: uuid.UUID, *, revision: int = 1) -> dict:
        return {
            "lease_id": f"viewer-{workspace_id}",
            "kind": "viewer",
            "owner_id": "viewer-owner",
            "revision": revision,
            "epoch": "fake-desktop-epoch",
        }

    async def test_desktop_action_emits_result(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["harness:desktop_action"]
        await handler(
            _payload(
                workspace_id,
                "d1",
                action="binding",
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
            [(workspace_id, "binding", {})],
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
                action="record_start",
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
        self.assertEqual(process_payload["guest_ip"], "172.22.0.2")

    async def test_idempotent_hold_keeps_tunnels(self) -> None:
        """Same session object (no restart) must not close healthy tunnels."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        from unittest.mock import MagicMock

        sentinel = MagicMock()
        sentinel.generation = 1
        sentinel.viewer_held = False
        sentinel.computeruse_run_ids = {"run-1"}
        service.desktop._desktop_sessions[workspace_id] = sentinel

        async def _idempotent_hold(ws_id, action, args=None):
            service.desktop_action_calls.append((ws_id, action, args))
            return {
                "ok": True,
                "display": ":1",
                "port": 6901,
                "viewer": False,
                "computer_use": True,
                "generation": 1,
                "epoch": "fake-desktop-epoch",
            }

        service.desktop.desktop_action = _idempotent_hold  # type: ignore[method-assign]
        closed: list = []

        async def _fake_close(ws_id, **_kwargs):
            closed.append(ws_id)

        interface._desktop_proxy.close_workspace = _fake_close  # type: ignore[assignment]
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
        service.desktop._desktop_sessions[workspace_id] = sentinel
        close_order: list = []
        closed: list = []
        orig_emit = interface._sio.emit

        async def _fake_close(ws_id, **_kwargs):
            closed.append(ws_id)
            close_order.append("close")

        interface._desktop_proxy.close_workspace = _fake_close  # type: ignore[assignment]

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
        self.assertIsNot(service.desktop._desktop_sessions[workspace_id], sentinel)
        # Other workspaces are untouched: scoping is per-workspace.
        self.assertNotIn(other_id, closed)

    async def test_release_kept_emits_process_release_stopped_closes_tunnels(
        self,
    ) -> None:
        """Kept releases announce process; final releases stop + close tunnels."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        from unittest.mock import MagicMock

        kept = MagicMock()
        kept.generation = 1
        service.desktop._desktop_sessions[workspace_id] = kept
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
            async def _desktop_action_impl(self, workspace_id, action, args=None):
                self.desktop_action_calls.append((workspace_id, action, args))
                return {
                    "ok": True,
                    "stopped": True,
                    "process_alive": False,
                    "viewer_held": False,
                    "computer_use_active": False,
                    "generation": 0,
                    "epoch": "fake-desktop-epoch",
                }

        service2 = StoppingService()
        interface2 = _interface(service2)
        interface2._desktop_proxy._tunnels["t1"] = object()
        closed: list = []

        async def _fake_close(ws_id, **_kwargs):
            closed.append(ws_id)

        interface2._desktop_proxy.close_workspace = _fake_close  # type: ignore[assignment]
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

    async def test_viewer_hold_does_not_close_healthy_tunnels(self) -> None:
        """An idempotent viewer hold keeps concurrent viewers attached."""
        from unittest.mock import MagicMock

        service = _FakeService()
        interface = _interface(service)
        interface._desktop_proxy._tunnels["t1"] = object()
        closed: list = []

        async def _fake_close(ws_id, **_kwargs):
            closed.append(ws_id)

        interface._desktop_proxy.close_workspace = _fake_close  # type: ignore[assignment]
        workspace_id = uuid.uuid4()
        session = MagicMock()
        session.port = 6901
        session.viewer_held = True
        session.computeruse_run_ids = set()
        session.generation = 1
        service.desktop._desktop_sessions[workspace_id] = session

        async def _idempotent_viewer_hold(ws_id, action, args=None):
            service.desktop_action_calls.append((ws_id, action, args))
            return {
                "ok": True,
                "port": 6901,
                "viewer": True,
                "computer_use": False,
                "generation": 1,
                "epoch": "fake-desktop-epoch",
            }

        service.desktop.desktop_action = _idempotent_viewer_hold  # type: ignore[method-assign]
        # Idempotent hold keeps the same session and does not close tunnels.
        await interface._sio.handlers["/"]["task:start_desktop"](
            {
                "task_id": "task-1",
                "workspace_id": str(workspace_id),
                **self._viewer_lease(workspace_id),
                "desktop_width": 1280,
                "desktop_height": 720,
            }
        )
        self.assertEqual(closed, [])
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:started", events)
        self.assertEqual(
            service.desktop_action_calls,
            [
                (
                    workspace_id,
                    "hold",
                    {
                        **self._viewer_lease(workspace_id),
                        "desktop_width": 1280,
                        "desktop_height": 720,
                    },
                )
            ],
        )

    async def test_viewer_hold_restart_closes_only_target_workspace(
        self,
    ) -> None:
        """Stale restart closes target tunnels before desktop:started."""
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        other_id = uuid.uuid4()
        sentinel = object()
        service.desktop._desktop_sessions[workspace_id] = sentinel
        service.desktop._desktop_sessions[other_id] = object()
        close_order: list = []
        closed: list = []
        orig_emit = interface._sio.emit

        async def _fake_close(ws_id, **_kwargs):
            closed.append(ws_id)
            close_order.append("close")

        interface._desktop_proxy.close_workspace = _fake_close  # type: ignore[assignment]

        async def _spy_emit(event, payload=None, *args, **kwargs):
            if event == "desktop:started":
                close_order.append("started")
            return await orig_emit(event, payload, *args, **kwargs)

        interface._sio.emit = _spy_emit  # type: ignore[method-assign]

        await interface._sio.handlers["/"]["task:start_desktop"](
            {
                "task_id": "task-2",
                "workspace_id": str(workspace_id),
                **self._viewer_lease(workspace_id),
            }
        )
        self.assertEqual(closed, [workspace_id])
        self.assertLess(
            close_order.index("close"),
            close_order.index("started"),
        )
        self.assertIsNot(service.desktop._desktop_sessions[workspace_id], sentinel)

    async def test_stop_desktop_releases_viewer_lease_and_keeps_computer_use(self) -> None:
        """Viewer release with computer-use hold keeps the process cached."""
        from unittest.mock import MagicMock

        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        session = MagicMock()
        session.generation = 2
        session.viewer_held = True
        session.computeruse_run_ids = {"run-1"}
        service.desktop._desktop_sessions[workspace_id] = session
        lease = self._viewer_lease(workspace_id, revision=3)
        await interface._sio.handlers["/"]["task:stop_desktop"](
            {"task_id": "task-2", "workspace_id": str(workspace_id), **lease}
        )
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:viewer_released", events)
        self.assertNotIn("desktop:stopped", events)
        self.assertEqual(
            service.desktop_action_calls,
            [(workspace_id, "release", lease)],
        )

    async def test_task_desktop_rejects_invalid_lease_identity(self) -> None:
        service = _FakeService()
        interface = _interface(service)
        workspace_id = uuid.uuid4()
        valid = self._viewer_lease(workspace_id)
        invalid = [{}, {**valid, "kind": "computeruse"}, {**valid, "revision": 0}]
        for task in ("start_desktop", "stop_desktop"):
            for lease in invalid:
                with self.subTest(task=task, lease=lease):
                    interface._sio.emit.reset_mock()
                    await interface._sio.handlers["/"][f"task:{task}"](
                        {"task_id": "invalid", "workspace_id": str(workspace_id), **lease}
                    )
                    self.assertEqual(service.desktop_action_calls, [])
                    event, error = interface._sio.emit.await_args.args
                    self.assertEqual(event, "workspace:error")
                    self.assertTrue(error["error"])

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
                    action="binding",
                    args={},
                )
            )
        )
        await asyncio.sleep(0)
        self.assertIn("harness:d-reg", interface._running_tasks)
        await task
        self.assertNotIn("harness:d-reg", interface._running_tasks)

    async def test_desktop_record_start_cancelled_by_harness_cancel(self) -> None:
        started = asyncio.Event()

        class BlockingService(_FakeService):
            async def _desktop_action_impl(self, workspace_id, action, args=None):
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
                    "d-record",
                    action="record_start",
                    args={"run_id": "run-1", "lease_id": "computeruse-lease"},
                )
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        self.assertIn("harness:d-record", interface._running_tasks)
        cancel = interface._sio.handlers["/"]["harness:cancel"]
        await cancel({"request_id": "d-record"})
        self.assertNotIn("harness:d-record", interface._running_tasks)
        await handler_task
        emitted_events = [
            call.args[0] for call in interface._sio.emit.await_args_list
        ]
        self.assertNotIn("harness:desktop_action_result", emitted_events)


class HarnessServiceSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_file_content_rejects_traversal(self) -> None:
        from src.service import WorkspaceService

        service = WorkspaceService(
            runtime={}, settings=RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        with self.assertRaises(ValueError):
            await service.files.write_file_content(
                uuid.uuid4(), "/etc/passwd", base64.b64encode(b"x").decode()
            )

    async def test_file_manager_rejects_external_paths_by_default(self) -> None:
        from src.models import WorkspaceInfo
        from src.service import WorkspaceService

        class UnusedRuntime:
            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                raise AssertionError(
                    "external paths must be rejected before runtime access"
                )

        workspace_id = uuid.uuid4()
        service = WorkspaceService(
            runtime=UnusedRuntime(),
            settings=RunnerSettings(state_dir=tempfile.mkdtemp()),
        )
        service.registry._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id="instance-1",
            status="running",
        )
        content = base64.b64encode(b"x").decode()

        for operation in (
            service.files.read_file(workspace_id, "/etc/passwd"),
            service.files.write_file_content(workspace_id, "/etc/passwd", content),
            service.files.list_files(workspace_id, "/etc"),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                await operation

    def test_parse_exec_output_splits_streams(self) -> None:
        from src.services.harness_exec import HarnessExecService

        stdout_b64 = base64.b64encode(b"out").decode()
        stderr_b64 = base64.b64encode(b"err").decode()
        output = (
            f"OPENCURIA_STDOUT\n{stdout_b64}\n"
            f"OPENCURIA_STDERR\n{stderr_b64}\nEXIT:0"
        )
        stdout, stderr = HarnessExecService._parse_harness_exec_output(output)
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
            runtime=runtime, settings=RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        workspace_id = uuid.uuid4()
        service.registry._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id="instance-1",
            status="running",
        )
        return service, captured, workspace_id

    async def test_read_file_quotes_single_quote_path(self) -> None:
        import shlex

        service, captured, workspace_id = self._service()
        evil = "/workspace/x'; touch /tmp/pwned; echo '"
        with self.assertRaises((RuntimeError, FileNotFoundError)):
            await service.files.read_file(workspace_id, evil)
        shell_cmd = next(c[2] for c in captured if c[:2] == ["sh", "-c"])
        self.assertIn(shlex.quote(evil), shell_cmd)
        self.assertNotIn("'x'; touch", shell_cmd.replace(shlex.quote(evil), ""))

    async def test_download_dir_passes_single_quote_path_as_argv(self) -> None:
        from src.models import WorkspaceInfo

        service, captured, workspace_id = self._service()

        class DirRuntime:
            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                if command[:2] == ["realpath", "-m"]:
                    return 0, command[2] + "\n"
                captured.append(command)
                if command[3] == "opencuria-file-download":
                    return 0, "directory\n"
                return 0, "3\nQUJD"

        service.registry._runtime = DirRuntime()
        info = service.registry._cache[workspace_id]
        service.registry._cache[workspace_id] = WorkspaceInfo(
            workspace_id=info.workspace_id,
            instance_id="instance-1",
            status="running",
        )
        evil = "/workspace/x'; touch /tmp/pwned; echo '"
        result = await service.files.download_file(workspace_id, evil)
        self.assertTrue(result["content"])
        argv = next(c for c in captured if c[3] == "opencuria-directory-download")
        self.assertEqual(argv[-3:-1], evil.rsplit("/", 1))


class HarnessServiceSymlinkEscapeTests(unittest.IsolatedAsyncioTestCase):
    """Symlink containment: realpath escapes fail closed with ValueError."""

    def _service(self, resolved: str):
        from src.models import WorkspaceInfo
        from src.service import WorkspaceService

        class FakeRuntime:
            def __init__(self) -> None:
                self.calls = 0
                self.commands: list[list[str]] = []
                self.archives: list[tuple[str, bytes]] = []

            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                self.calls += 1
                self.commands.append(command)
                if command[:2] == ["realpath", "-m"]:
                    return 0, resolved + "\n"
                if command[0] == "find":
                    return 0, "f\t3\t/etc/target.txt\n"
                if command[0] == "sh" and "test -f" in command[2]:
                    return 0, "3\ntext/plain\nYWJj\n"
                return 0, ""

            async def put_archive(self, instance_id, path, archive):
                self.archives.append((path, archive))

        runtime = FakeRuntime()
        service = WorkspaceService(
            runtime=runtime, settings=RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        workspace_id = uuid.uuid4()
        service.registry._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id="instance-1",
            status="running",
        )
        return service, runtime, workspace_id

    async def test_read_file_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc/shadow")
        with self.assertRaises(ValueError):
            await service.files.read_file(workspace_id, "/workspace/link")

    async def test_list_files_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc")
        with self.assertRaises(ValueError):
            await service.files.list_files(workspace_id, "/workspace/link")

    async def test_download_file_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc/shadow")
        with self.assertRaises(ValueError):
            await service.files.download_file(workspace_id, "/workspace/link")

    async def test_write_file_content_rejects_symlink_escape(self) -> None:
        service, _runtime, workspace_id = self._service("/etc/cron.d/evil")
        with self.assertRaises(ValueError):
            await service.files.write_file_content(
                workspace_id,
                "/workspace/link",
                base64.b64encode(b"x").decode(),
            )

    async def test_external_and_symlink_paths_allowed_when_requested(self) -> None:
        """All file operations can use external paths only when opted in."""
        for requested_path in ("/etc/target.txt", "/workspace/link"):
            with self.subTest(path=requested_path):
                service, runtime, workspace_id = self._service(
                    "/etc/target.txt"
                )
                manager = service.files
                encoded = base64.b64encode(b"written externally").decode()

                read_result = await manager.read_file(
                    workspace_id, requested_path, allow_external=True
                )
                listed = await manager.list_files(
                    workspace_id, requested_path, allow_external=True
                )
                await manager.write_file_content(
                    workspace_id,
                    requested_path,
                    encoded,
                    mode=0o600,
                    allow_external=True,
                )

                self.assertEqual(
                    base64.b64decode(read_result["content"]), b"abc"
                )
                self.assertEqual(read_result["size"], 3)
                self.assertEqual(listed[0]["path"], "/etc/target.txt")
                self.assertIn(
                    [
                        "find",
                        "/etc/target.txt",
                        "-maxdepth",
                        "1",
                        "-mindepth",
                        "1",
                        "-printf",
                        r"%y\t%s\t%p\n",
                    ],
                    runtime.commands,
                )
                self.assertTrue(
                    any(
                        command[0] == "sh"
                        and "test -f /etc/target.txt" in command[2]
                        for command in runtime.commands
                    )
                )
                self.assertIn(
                    ["chmod", "600", "/etc/target.txt"], runtime.commands
                )
                self.assertIn(["mkdir", "-p", "/etc"], runtime.commands)
                self.assertEqual(len(runtime.archives), 1)
                archive_path, archive_data = runtime.archives[0]
                self.assertEqual(archive_path, "/etc")
                with tarfile.open(
                    fileobj=io.BytesIO(archive_data), mode="r:"
                ) as archive:
                    member = archive.getmember("target.txt")
                    self.assertEqual(
                        archive.extractfile(member).read(),
                        b"written externally",
                    )

    async def test_normal_path_passes_realpath_check(self) -> None:
        service, runtime, workspace_id = self._service(
            "/workspace/sub/file.txt"
        )
        resolved = await service.files._realpath_under_workspace(
            service.registry.get_runtime(workspace_id),
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
            runtime=runtime, settings=RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        resolved = await service.files._realpath_under_workspace(
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

        service.harness.exec_harness_command = _slow  # type: ignore[assignment]
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
