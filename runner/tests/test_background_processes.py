"""Tests for background process management (service + websocket)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.models import WorkspaceInfo
from src.service import WorkspaceService


class FakeRuntime:
    """Fake runtime simulating detached processes without Docker."""

    def __init__(self) -> None:
        self.exec_command_wait = AsyncMock(side_effect=self._dispatch)
        self.calls: list[tuple[tuple, dict]] = []
        self.killed: list[str] = []
        self.alive: dict[int, bool] = {}
        self.exit_codes: dict[str, int | None] = {}
        self.next_pid = 100
        self.last_start_shell = ""

    async def _dispatch(self, instance_id, command=None, workdir=None, env=None):
        self.calls.append(((instance_id, command, workdir), {"env": env}))
        argv = list(command or [])
        shell = argv[-1] if argv else ""
        # Start wrapper: contains setsid + echo $!
        if "setsid bash -c" in shell and "echo $!" in shell:
            self.last_start_shell = shell
            pid = self.next_pid
            self.next_pid += 1
            self.alive[pid] = True
            return (0, f"{pid}\n")
        # liveness probe: kill -0 <pid>
        if shell.startswith("kill -0 "):
            pid = int(shell.split()[2])
            return (0, "") if self.alive.get(pid, False) else (1, "")
        # exit file read: cat <exitfile>
        if argv[:1] == ["cat"]:
            exit_path = argv[1]
            pid_key = exit_path.split("/")[-1].replace(".exit", "")
            code = self.exit_codes.get(pid_key)
            if code is None:
                return (1, "No such file")
            return (0, f"{code}\n")
        # kill signals: TERM is graceful (fake keeps process alive so the
        # service escalates), KILL always terminates.
        if "kill -KILL" in shell:
            self.killed.append(shell)
            for pid in list(self.alive):
                if str(pid) in shell:
                    self.alive[pid] = False
            return (0, "")
        if "kill -TERM" in shell:
            self.killed.append(shell)
            return (0, "")
        return (0, "")

    # Unused abstract surface for service tests.
    async def remove_workspace(self, instance_id: str) -> None:
        return None

    async def stop_workspace(self, instance_id: str) -> None:
        return None


def _service_with_workspace(runtime=None):
    runtime = runtime or FakeRuntime()
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


class BackgroundServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_start_list_get_stop(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id, "proc-1", "sleep 60", name="sleeper"
        )
        self.assertEqual(started["pid"], 100)
        self.assertIn("proc-1.log", started["log_path"])
        # setsid + env sourcing present in wrapper
        self.assertIn("setsid bash -c", runtime.last_start_shell)
        self.assertIn("/root/.opencuria-env.sh", runtime.last_start_shell)

        listed = await service.list_background_processes(ws_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "running")

        status = await service.get_background_status(ws_id, "proc-1")
        self.assertEqual(status["status"], "running")

        result = await service.stop_background_process(ws_id, "proc-1")
        self.assertTrue(result["stopped"])
        self.assertNotIn("proc-1", service._background_processes.get(ws_id, {}))

    async def test_exit_detection_short_lived_process(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        await service.start_background_process(ws_id, "proc-exit", "exit 3")
        pid = service._background_processes[ws_id]["proc-exit"].pid
        runtime.alive[pid] = False
        runtime.exit_codes["proc-exit"] = 3
        status = await service.get_background_status(ws_id, "proc-exit")
        self.assertEqual(status["status"], "exited")
        self.assertEqual(status["exit_code"], 3)

    async def test_kill_path_force(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        await service.start_background_process(ws_id, "proc-k", "sleep 999")
        runtime.exit_codes["proc-k"] = 143
        result = await service.stop_background_process(
            ws_id, "proc-k", force=True
        )
        self.assertTrue(result["stopped"])
        self.assertTrue(any("KILL" in cmd for cmd in runtime.killed))

    def test_exit_command_wrapped_in_subshell(self) -> None:
        shell = WorkspaceService._build_background_start_shell(
            "exit 3", "/tmp/x.log", "/tmp/x.exit"
        )
        self.assertIn("( exit 3 )", shell)
        self.assertIn("echo $?", shell)

    def test_exit_command_with_env_wrapped_in_subshell(self) -> None:
        shell = WorkspaceService._build_background_start_shell(
            "exit 3",
            "/tmp/x.log",
            "/tmp/x.exit",
            {"FOO": "bar"},
        )
        self.assertIn("( exit 3 )", shell)
        self.assertIn("export FOO=", shell)
        self.assertIn("echo $?", shell)

    def test_normal_command_wrapped_in_subshell(self) -> None:
        shell = WorkspaceService._build_background_start_shell(
            "echo hi", "/tmp/x.log", "/tmp/x.exit"
        )
        self.assertIn("( echo hi )", shell)
        self.assertIn("echo $?", shell)
        self.assertIn("setsid bash -c", shell)

    @unittest.skipIf(
        shutil.which("setsid") is None or shutil.which("bash") is None,
        "requires setsid and bash",
    )
    def test_exit_code_survives_shell_terminating_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = str(Path(tmp) / "proc-exit3.log")
            exit_path = str(Path(tmp) / "proc-exit3.exit")
            with unittest.mock.patch(
                "src.service.BACKGROUND_PROCESS_DIR", tmp
            ):
                shell = WorkspaceService._build_background_start_shell(
                    "exit 3", log_path, exit_path
                )
            result = subprocess.run(
                ["bash", "-lc", shell],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            deadline = time.time() + 5.0
            content = ""
            while time.time() < deadline:
                try:
                    content = Path(exit_path).read_text().strip()
                except FileNotFoundError:
                    time.sleep(0.05)
                    continue
                if content:
                    break
                time.sleep(0.05)
            self.assertEqual(content, "3")

    async def test_workspace_stop_kills_all(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        service.remove_workspace_credentials = AsyncMock()
        runtime.stop_workspace = AsyncMock()
        await service.start_background_process(ws_id, "p1", "sleep 10")
        await service.start_background_process(ws_id, "p2", "sleep 10")
        self.assertEqual(len(service._background_processes[ws_id]), 2)
        await service.stop_workspace(ws_id)
        self.assertNotIn(ws_id, service._background_processes)
        self.assertTrue(runtime.killed)

    async def test_unknown_process_id_raises(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        with self.assertRaises(ValueError):
            await service.get_background_status(ws_id, "nope")
        with self.assertRaises(ValueError):
            await service.stop_background_process(ws_id, "nope")

    async def test_empty_command_raises(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        with self.assertRaises(ValueError):
            await service.start_background_process(ws_id, "p-empty", "   ")

    async def test_heartbeat_includes_processes(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        await service.start_background_process(ws_id, "p-hb", "sleep 5")
        payload = await service.get_workspace_heartbeat_statuses()
        entry = next(
            item
            for item in payload
            if item["workspace_id"] == str(ws_id)
        )
        self.assertIn("processes", entry)
        self.assertEqual(entry["processes"][0]["process_id"], "p-hb")
        self.assertEqual(entry["processes"][0]["status"], "running")

    async def test_remove_workspace_cleans_processes(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        await service.start_background_process(ws_id, "p-rm", "sleep 5")
        await service.remove_workspace(ws_id)
        self.assertNotIn(ws_id, service._background_processes)


class BackgroundWebsocketTests(unittest.IsolatedAsyncioTestCase):
    def _interface(self, service) -> WebSocketInterface:
        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()
        return interface

    async def test_process_handlers_roundtrip(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        interface = self._interface(service)
        handlers = interface._sio.handlers["/"]

        await handlers["harness:process_start"](
            {
                "workspace_id": str(ws_id),
                "request_id": "r1",
                "process_id": "proc-ws",
                "command": "sleep 30",
                "workdir": "/workspace",
                "env": {},
                "name": "demo",
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_start_result")
        self.assertEqual(payload["process_id"], "proc-ws")
        self.assertEqual(payload["status"], "running")

        await handlers["harness:process_list"](
            {"workspace_id": str(ws_id), "request_id": "r2"}
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_list_result")
        self.assertEqual(len(payload["processes"]), 1)

        await handlers["harness:process_get"](
            {
                "workspace_id": str(ws_id),
                "request_id": "r3",
                "process_id": "proc-ws",
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_get_result")
        self.assertEqual(payload["process"]["status"], "running")

        await handlers["harness:process_stop"](
            {
                "workspace_id": str(ws_id),
                "request_id": "r4",
                "process_id": "proc-ws",
                "force": True,
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_stop_result")
        self.assertTrue(payload["stopped"])

    async def test_process_start_error_has_error_field(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        interface = self._interface(service)
        handlers = interface._sio.handlers["/"]
        await handlers["harness:process_start"](
            {
                "workspace_id": str(ws_id),
                "request_id": "re",
                "process_id": "proc-err",
                "command": "   ",
                "workdir": "/workspace",
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_start_result")
        self.assertIn("error", payload)

    async def test_process_get_unknown_reports_error(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        interface = self._interface(service)
        handlers = interface._sio.handlers["/"]
        await handlers["harness:process_get"](
            {
                "workspace_id": str(ws_id),
                "request_id": "ru",
                "process_id": "missing",
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_get_result")
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
