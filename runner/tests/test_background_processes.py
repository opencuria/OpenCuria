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
from typing import Any
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.models import WorkspaceInfo
from src.service import WorkspaceService
from src.services.sessions.background import (
    BACKGROUND_PROCESS_DIR,
    BackgroundProcessManager,
)


class FakeRuntime:
    """Fake runtime simulating detached processes without a VM."""

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

    async def get_workspace_status(self, instance_id):
        from types import SimpleNamespace
        return SimpleNamespace(status="running")

    # Unused abstract surface for service tests.
    async def remove_workspace(self, instance_id: str) -> None:
        return None

    async def stop_workspace(self, instance_id: str) -> None:
        return None


def _service_with_workspace(runtime=None):
    runtime = runtime or FakeRuntime()
    state_dir = tempfile.mkdtemp()
    service = WorkspaceService(
        runtime=runtime,
        settings=RunnerSettings(state_dir=state_dir),
    )
    service.configure_desktop_leases(state_dir, "background-test-epoch")
    workspace_id = uuid.uuid4()
    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
    )
    return service, runtime, workspace_id


async def _start_process(
    service: WorkspaceService,
    workspace_id: uuid.UUID,
    process_id: str,
    command: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Supply the same explicit paths as the backend's process RPC."""
    return await service.background.start_background_process(
        workspace_id,
        process_id,
        command,
        **{
            "log_path": f"{BACKGROUND_PROCESS_DIR}/{process_id}.log",
            "exit_path": f"{BACKGROUND_PROCESS_DIR}/{process_id}.exit",
            **kwargs,
        },
    )


class BackgroundServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_start_list_get_stop(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await _start_process(
            service, ws_id, "proc-1", "sleep 60", name="sleeper"
        )
        self.assertEqual(started["pid"], 100)
        self.assertIn("proc-1.log", started["log_path"])
        # setsid + env sourcing present in wrapper
        self.assertIn("setsid bash -c", runtime.last_start_shell)
        self.assertIn("/root/.opencuria-env.sh", runtime.last_start_shell)

        listed = await service.background.list_background_processes(ws_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "running")

        status = await service.background.get_background_status(ws_id, "proc-1")
        self.assertEqual(status["status"], "running")

        result = await service.background.stop_background_process(ws_id, "proc-1")
        self.assertTrue(result["stopped"])
        self.assertNotIn("proc-1", service.background._background_processes.get(ws_id, {}))

    async def test_exit_detection_short_lived_process(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        await _start_process(service, ws_id, "proc-exit", "exit 3")
        pid = service.background._background_processes[ws_id]["proc-exit"].pid
        runtime.alive[pid] = False
        runtime.exit_codes["proc-exit"] = 3
        status = await service.background.get_background_status(ws_id, "proc-exit")
        self.assertEqual(status["status"], "exited")
        self.assertEqual(status["exit_code"], 3)

    async def test_stop_escalates_to_kill_after_grace(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        runtime.ignore_term = True
        await _start_process(service, ws_id, "proc-k", "sleep 999")
        runtime.exit_codes["proc-k"] = 143
        with unittest.mock.patch(
            "src.services.sessions.background.asyncio.sleep", new_callable=AsyncMock
        ):
            result = await service.background.stop_background_process(ws_id, "proc-k")
        self.assertTrue(result["stopped"])
        self.assertTrue(any("TERM" in cmd for cmd in runtime.killed))
        self.assertTrue(any("KILL" in cmd for cmd in runtime.killed))

    @unittest.skipIf(
        shutil.which("setsid") is None or shutil.which("bash") is None,
        "requires setsid and bash",
    )
    def test_exit_code_survives_shell_terminating_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = str(Path(tmp) / "proc-exit3.log")
            exit_path = str(Path(tmp) / "proc-exit3.exit")
            with unittest.mock.patch(
                "src.services.sessions.background.BACKGROUND_PROCESS_DIR", tmp
            ):
                shell = BackgroundProcessManager._build_background_start_shell(
                    'exit "$RESULT"', log_path, exit_path, {"RESULT": "3"}
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
        service.credentials.remove_workspace_credentials = AsyncMock()
        runtime.stop_workspace = AsyncMock()
        await _start_process(service, ws_id, "p1", "sleep 10")
        await _start_process(service, ws_id, "p2", "sleep 10")
        self.assertEqual(len(service.background._background_processes[ws_id]), 2)
        await service.lifecycle.stop_workspace(ws_id)
        self.assertNotIn(ws_id, service.background._background_processes)
        self.assertTrue(runtime.killed)

    async def test_unknown_process_id_raises(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        with self.assertRaises(ValueError):
            await service.background.get_background_status(ws_id, "nope")
        with self.assertRaises(ValueError):
            await service.background.stop_background_process(ws_id, "nope")

    async def test_empty_command_raises(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        with self.assertRaises(ValueError):
            await _start_process(service, ws_id, "p-empty", "   ")

    async def test_heartbeat_skips_process_probes_for_stopped_workspace(self) -> None:
        """A stopped runtime is definitive; process liveness probes are redundant."""
        service, runtime, ws_id = _service_with_workspace()
        await _start_process(service, ws_id, "p-stopped", "sleep 5")
        service.registry._cache[ws_id].status = "exited"
        runtime.exec_command_wait.reset_mock()

        payload = await service.registry.get_workspace_heartbeat_statuses()

        entry = next(item for item in payload if item["workspace_id"] == str(ws_id))
        self.assertEqual(entry["processes"], [])
        runtime.exec_command_wait.assert_not_awaited()

    async def test_heartbeat_includes_processes(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        await _start_process(service, ws_id, "p-hb", "sleep 5")
        payload = await service.registry.get_workspace_heartbeat_statuses()
        entry = next(item for item in payload if item["workspace_id"] == str(ws_id))
        self.assertIn("processes", entry)
        self.assertEqual(entry["processes"][0]["process_id"], "p-hb")
        self.assertEqual(entry["processes"][0]["status"], "running")

    async def test_remove_workspace_cleans_processes(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        await _start_process(service, ws_id, "p-rm", "sleep 5")
        await service.lifecycle.remove_workspace(ws_id)
        self.assertNotIn(ws_id, service.background._background_processes)

    async def test_restart_replaces_entry_and_stops_old_pid(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        await _start_process(service, ws_id, "proc-re", "sleep 60")
        old_pid = service.background._background_processes[ws_id]["proc-re"].pid
        self.assertTrue(runtime.alive.get(old_pid))
        await _start_process(
            service,
            ws_id,
            "proc-re",
            "sleep 60",
            log_path=f"{BACKGROUND_PROCESS_DIR}/proc-re_r2.log",
            exit_path=f"{BACKGROUND_PROCESS_DIR}/proc-re_r2.exit",
        )
        self.assertEqual(len(service.background._background_processes[ws_id]), 1)
        entry = service.background._background_processes[ws_id]["proc-re"]
        self.assertEqual(entry.log_path, f"{BACKGROUND_PROCESS_DIR}/proc-re_r2.log")
        self.assertNotEqual(entry.pid, old_pid)
        self.assertFalse(runtime.alive.get(old_pid))


class BackgroundWebsocketTests(unittest.IsolatedAsyncioTestCase):
    def _interface(self, service) -> WebSocketInterface:
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
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
                "log_path": f"{BACKGROUND_PROCESS_DIR}/proc-ws_r2.log",
                "exit_path": f"{BACKGROUND_PROCESS_DIR}/proc-ws_r2.exit",
                "run_count": 2,
            }
        )
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "harness:process_start_result")
        self.assertEqual(payload["process_id"], "proc-ws")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(
            payload["log_path"], f"{BACKGROUND_PROCESS_DIR}/proc-ws_r2.log"
        )
        self.assertEqual(
            payload["exit_path"], f"{BACKGROUND_PROCESS_DIR}/proc-ws_r2.exit"
        )
        self.assertEqual(payload["run_count"], 2)
        entry = service.background._background_processes[ws_id]["proc-ws"]
        self.assertEqual(entry.log_path, payload["log_path"])

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

    async def test_process_start_error_has_error_field(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        interface = self._interface(service)
        handlers = interface._sio.handlers["/"]
        await handlers["harness:process_start"](
            {
                "workspace_id": str(ws_id),
                "request_id": "re",
                "process_id": "proc-err",
                "log_path": f"{BACKGROUND_PROCESS_DIR}/proc-err.log",
                "exit_path": f"{BACKGROUND_PROCESS_DIR}/proc-err.exit",
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
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()
        return interface

    async def test_verify_reattaches_live_process(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await _start_process(service, ws_id, "proc-re", "sleep 60")
        pid = started["pid"]
        # Simulate a runner restart: tracking is gone (in-memory only)
        # but the setsid process lives on inside the workspace.
        service.background._background_processes.pop(ws_id, None)
        self.assertTrue(runtime.alive.get(pid))

        results = await service.background.verify_and_reattach_background_processes(
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
        status = await service.background.get_background_status(ws_id, "proc-re")
        self.assertEqual(status["status"], "running")

    async def test_verify_is_idempotent(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await _start_process(service, ws_id, "proc-idem", "sleep 60")
        candidate = {
            "process_id": "proc-idem",
            "pid": started["pid"],
            "log_path": started["log_path"],
            "exit_path": started["exit_path"],
        }
        service.background._background_processes.pop(ws_id, None)
        first = await service.background.verify_and_reattach_background_processes(
            ws_id, [candidate]
        )
        second = await service.background.verify_and_reattach_background_processes(
            ws_id, [candidate]
        )
        self.assertEqual(first[0]["status"], "running")
        self.assertEqual(second[0]["status"], "running")
        self.assertEqual(len(service.background._background_processes[ws_id]), 1)

    async def test_verify_exited_reports_exit_code_without_tracking(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await _start_process(service, ws_id, "proc-ex", "exit 3")
        pid = started["pid"]
        runtime.alive[pid] = False
        runtime.exit_codes["proc-ex"] = 3
        service.background._background_processes.pop(ws_id, None)

        results = await service.background.verify_and_reattach_background_processes(
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
        self.assertNotIn(ws_id, service.background._background_processes)

    async def test_verify_unknown_without_exit_code(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        results = await service.background.verify_and_reattach_background_processes(
            ws_id,
            [
                {
                    "process_id": "proc-gone",
                    "pid": 4242,
                    "log_path": f"{BACKGROUND_PROCESS_DIR}/proc-gone.log",
                    "exit_path": f"{BACKGROUND_PROCESS_DIR}/proc-gone.exit",
                }
            ],
        )
        self.assertEqual(results[0]["status"], "unknown")
        self.assertIsNone(results[0]["exit_code"])
        self.assertNotIn(ws_id, service.background._background_processes)

    async def test_verify_rejects_invalid_process_id_and_pid(self) -> None:
        service, _runtime, ws_id = _service_with_workspace()
        results = await service.background.verify_and_reattach_background_processes(
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
        self.assertNotIn(ws_id, service.background._background_processes)

    async def test_verify_handler_roundtrip(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        started = await _start_process(service, ws_id, "proc-h", "sleep 30")
        service.background._background_processes.pop(ws_id, None)
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
        await _start_process(service, ws_id, "p1", "sleep 10")
        await _start_process(service, ws_id, "p2", "sleep 10")
        calls_before = len(runtime.calls)
        with unittest.mock.patch(
            "src.services.sessions.background.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock:
            await service.background._kill_all_background_processes(ws_id, reason="test")
        # Grace polling happened (sleeps) before KILL was sent.
        self.assertTrue(sleep_mock.await_count >= 1)
        self.assertTrue(any("TERM" in cmd for cmd in runtime.killed))
        self.assertTrue(any("KILL" in cmd for cmd in runtime.killed))
        self.assertGreater(len(runtime.calls), calls_before)
        self.assertNotIn(ws_id, service.background._background_processes)

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
        await _start_process(service, ws_id, "p1", "sleep 10")
        self.assertIn(ws_id, service.background._background_processes)
        # Evict the cache entry (e.g. sync_from_runtime dropped it while
        # in-memory background tracking survived the restart window).
        service.registry._cache.pop(ws_id, None)
        calls_before = len(runtime.calls)

        await service.background._kill_all_background_processes(ws_id, reason="test")

        self.assertNotIn(ws_id, service.background._background_processes)
        # Nothing left to signal: no runtime probes/kills after eviction.
        self.assertEqual(len(runtime.calls), calls_before)

    async def test_kill_all_propagates_unexpected_lookup_errors(self) -> None:
        """_kill_all does not swallow unexpected errors from the lookups."""
        service, _runtime, ws_id = _service_with_workspace()
        await _start_process(service, ws_id, "p1", "sleep 10")

        def _boom(_workspace_id: uuid.UUID):
            raise OSError("store exploded")

        service.background._get_cached = _boom  # type: ignore[method-assign]
        service.background._get_runtime = _boom  # type: ignore[method-assign]

        with self.assertRaises(OSError):
            await service.background._kill_all_background_processes(ws_id, reason="test")

    async def test_remove_workspace_with_evicted_cache_is_rejected(self) -> None:
        """Removal must preserve tracking when physical identity is unavailable."""
        service, runtime, ws_id = _service_with_workspace()
        await _start_process(service, ws_id, "p1", "sleep 10")
        service.registry._cache.pop(ws_id, None)
        calls_before = len(runtime.calls)

        with self.assertRaisesRegex(RuntimeError, "physical registry identity"):
            await service.lifecycle.remove_workspace(ws_id)

        self.assertIn(ws_id, service.background._background_processes)
        self.assertTrue(runtime.alive[100])
        self.assertEqual(len(runtime.calls), calls_before)

    async def test_concurrent_same_id_starts_do_not_orphan(self) -> None:
        service, runtime, ws_id = _service_with_workspace()
        await asyncio.gather(
            _start_process(service, ws_id, "proc-race", "sleep 60"),
            _start_process(service, ws_id, "proc-race", "sleep 60"),
        )
        entries = service.background._background_processes[ws_id]
        self.assertEqual(len(entries), 1)
        winner_pid = entries["proc-race"].pid
        # The loser's PID was stopped; only the winner is alive.
        alive_pids = [pid for pid, flag in runtime.alive.items() if flag]
        self.assertEqual(alive_pids, [winner_pid])


@pytest.mark.parametrize("field", ["log_path", "exit_path"])
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "",
        "/etc/evil.log",
        f"{BACKGROUND_PROCESS_DIR}/../evil.log",
        f"{BACKGROUND_PROCESS_DIR}/nested/evil.log",
        f"{BACKGROUND_PROCESS_DIR}/evil.txt",
    ],
)
async def test_invalid_paths_never_start_or_reattach(
    field: str, invalid: str | None
) -> None:
    """Reject obsolete/malformed payloads before runtime calls or tracking."""
    service, runtime, workspace_id = _service_with_workspace()
    interface = WebSocketInterface(
        service, RunnerSettings(state_dir=tempfile.mkdtemp())
    )
    interface._sio.emit = AsyncMock()
    candidate = {
        "process_id": "proc-invalid",
        "pid": 123,
        "log_path": f"{BACKGROUND_PROCESS_DIR}/proc-invalid.log",
        "exit_path": f"{BACKGROUND_PROCESS_DIR}/proc-invalid.exit",
    }
    if invalid is None:
        candidate.pop(field)
    else:
        candidate[field] = invalid
    await interface._sio.handlers["/"]["harness:process_start"](
        {**candidate, "workspace_id": str(workspace_id), "command": "sleep 60"}
    )
    assert "error" in interface._sio.emit.await_args.args[1]
    result = await service.background.verify_and_reattach_background_processes(
        workspace_id, [candidate]
    )
    assert result[0]["status"] == "unknown"
    assert "error" in result[0]
    runtime.exec_command_wait.assert_not_awaited()
    assert not service.background._background_processes
