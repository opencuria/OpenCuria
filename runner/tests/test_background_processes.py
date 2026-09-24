"""Tests for background process management (service + websocket)."""

from __future__ import annotations

import asyncio
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
        self.ignore_term = False

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
            if not self.ignore_term:
                for pid in list(self.alive):
                    if str(pid) in shell:
                        self.alive[pid] = False
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

    async def test_stop_escalates_to_kill_after_grace(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        runtime.ignore_term = True
        await service.start_background_process(ws_id, "proc-k", "sleep 999")
        runtime.exit_codes["proc-k"] = 143
        with unittest.mock.patch(
            "src.service.asyncio.sleep", new_callable=AsyncMock
        ):
            result = await service.stop_background_process(ws_id, "proc-k")
        self.assertTrue(result["stopped"])
        self.assertTrue(any("TERM" in cmd for cmd in runtime.killed))
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

    async def test_explicit_run_paths_used(self) -> None:
        from src.service import BACKGROUND_PROCESS_DIR

        service, runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id,
            "proc-r",
            "sleep 60",
            log_path=f"{BACKGROUND_PROCESS_DIR}/proc-r_r2.log",
            exit_path=f"{BACKGROUND_PROCESS_DIR}/proc-r_r2.exit",
        )
        self.assertEqual(
            started["log_path"], f"{BACKGROUND_PROCESS_DIR}/proc-r_r2.log"
        )
        self.assertEqual(
            started["exit_path"], f"{BACKGROUND_PROCESS_DIR}/proc-r_r2.exit"
        )
        entry = service._background_processes[ws_id]["proc-r"]
        self.assertEqual(entry.log_path, started["log_path"])
        self.assertIn("proc-r_r2.log", runtime.last_start_shell)

    async def test_legacy_paths_without_explicit(self) -> None:
        from src.service import BACKGROUND_PROCESS_DIR

        service, _runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id, "proc-leg", "sleep 60"
        )
        self.assertEqual(
            started["log_path"], f"{BACKGROUND_PROCESS_DIR}/proc-leg.log"
        )
        self.assertEqual(
            started["exit_path"], f"{BACKGROUND_PROCESS_DIR}/proc-leg.exit"
        )

    async def test_restart_replaces_entry_and_stops_old_pid(self) -> None:
        from src.service import BACKGROUND_PROCESS_DIR

        service, runtime, ws_id = _service_with_workspace()
        await service.start_background_process(ws_id, "proc-re", "sleep 60")
        old_pid = service._background_processes[ws_id]["proc-re"].pid
        self.assertTrue(runtime.alive.get(old_pid))
        await service.start_background_process(
            ws_id,
            "proc-re",
            "sleep 60",
            log_path=f"{BACKGROUND_PROCESS_DIR}/proc-re_r2.log",
            exit_path=f"{BACKGROUND_PROCESS_DIR}/proc-re_r2.exit",
        )
        self.assertEqual(len(service._background_processes[ws_id]), 1)
        entry = service._background_processes[ws_id]["proc-re"]
        self.assertEqual(
            entry.log_path, f"{BACKGROUND_PROCESS_DIR}/proc-re_r2.log"
        )
        self.assertNotEqual(entry.pid, old_pid)
        self.assertFalse(runtime.alive.get(old_pid))

    async def test_invalid_log_path_falls_back_to_legacy(self) -> None:
        from src.service import BACKGROUND_PROCESS_DIR

        service, _runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id,
            "proc-inv",
            "sleep 60",
            log_path="/etc/x.log",
            exit_path="/etc/x.exit",
        )
        self.assertEqual(
            started["log_path"], f"{BACKGROUND_PROCESS_DIR}/proc-inv.log"
        )
        self.assertEqual(
            started["exit_path"], f"{BACKGROUND_PROCESS_DIR}/proc-inv.exit"
        )

    async def test_invalid_traversal_log_path_falls_back(self) -> None:
        from src.service import BACKGROUND_PROCESS_DIR

        service, _runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id,
            "proc-trav",
            "sleep 60",
            log_path=f"{BACKGROUND_PROCESS_DIR}/../evil.log",
            exit_path=f"{BACKGROUND_PROCESS_DIR}/../evil.exit",
        )
        self.assertEqual(
            started["log_path"], f"{BACKGROUND_PROCESS_DIR}/proc-trav.log"
        )
        self.assertEqual(
            started["exit_path"], f"{BACKGROUND_PROCESS_DIR}/proc-trav.exit"
        )


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
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_stop_result")
        self.assertTrue(payload["stopped"])

    async def test_process_start_passthrough_run_paths_and_count(self) -> None:
        from src.service import BACKGROUND_PROCESS_DIR

        service, _runtime, ws_id = _service_with_workspace()
        interface = self._interface(service)
        handlers = interface._sio.handlers["/"]
        await handlers["harness:process_start"](
            {
                "workspace_id": str(ws_id),
                "request_id": "r-run",
                "process_id": "proc-ws2",
                "command": "sleep 30",
                "workdir": "/workspace",
                "env": {},
                "name": "demo",
                "log_path": f"{BACKGROUND_PROCESS_DIR}/proc-ws2_r2.log",
                "exit_path": f"{BACKGROUND_PROCESS_DIR}/proc-ws2_r2.exit",
                "run_count": 2,
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_start_result")
        self.assertEqual(
            payload["log_path"], f"{BACKGROUND_PROCESS_DIR}/proc-ws2_r2.log"
        )
        self.assertEqual(
            payload["exit_path"], f"{BACKGROUND_PROCESS_DIR}/proc-ws2_r2.exit"
        )
        self.assertEqual(payload["run_count"], 2)
        entry = service._background_processes[ws_id]["proc-ws2"]
        self.assertEqual(entry.log_path, payload["log_path"])

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


class BackgroundVerifyTests(unittest.IsolatedAsyncioTestCase):
    """Verify/reattach after tracking loss (e.g. runner restart)."""

    def _interface(self, service) -> WebSocketInterface:
        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()
        return interface


    async def test_verify_reattaches_live_process(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id, "proc-re", "sleep 60"
        )
        pid = started["pid"]
        # Simulate a runner restart: tracking is gone (in-memory only)
        # but the setsid process lives on inside the workspace.
        service._background_processes.pop(ws_id, None)
        self.assertTrue(runtime.alive.get(pid))

        results = await service.verify_and_reattach_background_processes(
            ws_id,
            [
                {
                    "process_id": "proc-re",
                    "pid": pid,
                    "log_path": started["log_path"],
                    "exit_path": started["exit_path"],
                }
            ],
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "running")
        self.assertEqual(results[0]["pid"], pid)
        # Reattached: status lookups work again without an error.
        status = await service.get_background_status(ws_id, "proc-re")
        self.assertEqual(status["status"], "running")

    async def test_verify_is_idempotent(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id, "proc-idem", "sleep 60"
        )
        candidate = {
            "process_id": "proc-idem",
            "pid": started["pid"],
            "log_path": started["log_path"],
            "exit_path": started["exit_path"],
        }
        service._background_processes.pop(ws_id, None)
        first = await service.verify_and_reattach_background_processes(
            ws_id, [candidate]
        )
        second = await service.verify_and_reattach_background_processes(
            ws_id, [candidate]
        )
        self.assertEqual(first[0]["status"], "running")
        self.assertEqual(second[0]["status"], "running")
        self.assertEqual(len(service._background_processes[ws_id]), 1)

    async def test_verify_exited_reports_exit_code_without_tracking(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id, "proc-ex", "exit 3"
        )
        pid = started["pid"]
        runtime.alive[pid] = False
        runtime.exit_codes["proc-ex"] = 3
        service._background_processes.pop(ws_id, None)

        results = await service.verify_and_reattach_background_processes(
            ws_id,
            [
                {
                    "process_id": "proc-ex",
                    "pid": pid,
                    "log_path": started["log_path"],
                    "exit_path": started["exit_path"],
                }
            ],
        )
        self.assertEqual(results[0]["status"], "exited")
        self.assertEqual(results[0]["exit_code"], 3)
        # Dead processes are never reattached.
        self.assertNotIn(ws_id, service._background_processes)

    async def test_verify_unknown_without_exit_code(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        results = await service.verify_and_reattach_background_processes(
            ws_id,
            [{"process_id": "proc-gone", "pid": 4242}],
        )
        self.assertEqual(results[0]["status"], "unknown")
        self.assertIsNone(results[0]["exit_code"])
        self.assertNotIn(ws_id, service._background_processes)

    async def test_verify_rejects_path_traversal(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id, "proc-safe", "sleep 60"
        )
        service._background_processes.pop(ws_id, None)
        results = await service.verify_and_reattach_background_processes(
            ws_id,
            [
                {
                    "process_id": "proc-safe",
                    "pid": started["pid"],
                    "log_path": "/etc/evil.log",
                    "exit_path": "/etc/evil.exit",
                }
            ],
        )
        # The hostile paths fall back to the legacy schema, which has no
        # exit file in the fake runtime — but the live PID still
        # reattaches under the sanitized legacy paths.
        self.assertEqual(results[0]["status"], "running")
        entry = service._background_processes[ws_id]["proc-safe"]
        self.assertTrue(entry.log_path.startswith("/workspace/.opencuria/"))

    async def test_verify_rejects_invalid_process_id_and_pid(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        results = await service.verify_and_reattach_background_processes(
            ws_id,
            [
                {"process_id": "../evil", "pid": 100},
                {"process_id": "proc-badpid", "pid": "not-a-pid"},
            ],
        )
        self.assertEqual(results[0]["status"], "unknown")
        self.assertIn("error", results[0])
        self.assertEqual(results[1]["status"], "unknown")
        self.assertIn("error", results[1])
        self.assertNotIn(ws_id, service._background_processes)

    async def test_verify_handler_roundtrip(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await service.start_background_process(
            ws_id, "proc-h", "sleep 30"
        )
        service._background_processes.pop(ws_id, None)
        interface = self._interface(service)
        handlers = interface._sio.handlers["/"]
        await handlers["harness:process_verify"](
            {
                "workspace_id": str(ws_id),
                "request_id": "rv1",
                "expected": [
                    {
                        "process_id": "proc-h",
                        "pid": started["pid"],
                        "log_path": started["log_path"],
                        "exit_path": started["exit_path"],
                    }
                ],
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_verify_result")
        self.assertEqual(payload["request_id"], "rv1")
        self.assertEqual(payload["processes"][0]["status"], "running")

    async def test_verify_handler_rejects_bad_payload(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        interface = self._interface(service)
        handlers = interface._sio.handlers["/"]
        await handlers["harness:process_verify"](
            {"workspace_id": "not-a-uuid", "request_id": "rv-bad", "expected": []}
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_verify_result")
        self.assertEqual(payload["processes"], [])
        self.assertIn("error", payload)

    async def test_kill_all_has_term_grace_before_kill(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        runtime.ignore_term = True
        await service.start_background_process(ws_id, "p1", "sleep 10")
        await service.start_background_process(ws_id, "p2", "sleep 10")
        calls_before = len(runtime.calls)
        with unittest.mock.patch(
            "src.service.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock:
            await service._kill_all_background_processes(ws_id, reason="test")
        # Grace polling happened (sleeps) before KILL was sent.
        self.assertTrue(sleep_mock.await_count >= 1)
        self.assertTrue(any("TERM" in cmd for cmd in runtime.killed))
        self.assertTrue(any("KILL" in cmd for cmd in runtime.killed))
        self.assertGreater(len(runtime.calls), calls_before)
        self.assertNotIn(ws_id, service._background_processes)

    async def test_kill_all_unknown_workspace_drops_tracking_without_raising(
        self,
    ) -> None:
        """_kill_all stays best-effort when the cache no longer has the id.

        Regression test for the WorkspaceService decomposition (54c0a70):
        the old body used tolerant ``self._cache.get(...)`` /
        ``self._runtimes.get(...)`` lookups, so orphaned tracking
        (workspace evicted by ``sync_from_runtime`` or a double remove)
        logged ``background_processes_kill_skipped`` and dropped the
        tracking. The split must preserve that: no raise, no runtime
        touch, tracking cleared.
        """
        service, runtime, ws_id = _service_with_workspace()
        await service.start_background_process(ws_id, "p1", "sleep 10")
        self.assertIn(ws_id, service._background_processes)
        # Evict the cache entry (e.g. sync_from_runtime dropped it while
        # in-memory background tracking survived the restart window).
        service._cache.pop(ws_id, None)
        calls_before = len(runtime.calls)

        await service._kill_all_background_processes(ws_id, reason="test")

        self.assertNotIn(ws_id, service._background_processes)
        # Nothing left to signal: no runtime probes/kills after eviction.
        self.assertEqual(len(runtime.calls), calls_before)

    async def test_kill_all_propagates_unexpected_lookup_errors(self) -> None:
        """_kill_all does not swallow unexpected errors from the lookups."""
        service, _runtime, ws_id = _service_with_workspace()
        await service.start_background_process(ws_id, "p1", "sleep 10")

        def _boom(_workspace_id: uuid.UUID):
            raise OSError("store exploded")

        service._background._get_cached = _boom  # type: ignore[method-assign]
        service._background._get_runtime = _boom  # type: ignore[method-assign]

        with self.assertRaises(OSError):
            await service._kill_all_background_processes(
                ws_id, reason="test"
            )

    async def test_remove_workspace_with_evicted_cache_still_cleans_up(
        self,
    ) -> None:
        """remove_workspace completes when the cache was already evicted.

        Covers the teardown fan-out through the real kill-all path
        (previously only exercised with a mocked kill-all): orphaned
        background tracking is dropped and the removal returns normally.
        """
        service, _runtime, ws_id = _service_with_workspace()
        await service.start_background_process(ws_id, "p1", "sleep 10")
        service._cache.pop(ws_id, None)

        await service.remove_workspace(ws_id)

        self.assertNotIn(ws_id, service._background_processes)
        self.assertNotIn(ws_id, service._cache)

    async def test_concurrent_same_id_starts_do_not_orphan(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        await asyncio.gather(
            service.start_background_process(ws_id, "proc-race", "sleep 60"),
            service.start_background_process(ws_id, "proc-race", "sleep 60"),
        )
        entries = service._background_processes[ws_id]
        self.assertEqual(len(entries), 1)
        winner_pid = entries["proc-race"].pid
        # The loser's PID was stopped; only the winner is alive.
        alive_pids = [pid for pid, flag in runtime.alive.items() if flag]
        self.assertEqual(alive_pids, [winner_pid])


if __name__ == "__main__":
    unittest.main()
