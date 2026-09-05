import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.models import DesktopSession, WorkspaceInfo
from src.service import WorkspaceService


class DummyRuntime:
    def __init__(self) -> None:
        self.exec_command_wait = AsyncMock()

    def get_container_ip(self, instance_id: str, workspace_id: str) -> str:
        return "172.22.0.2"

    def get_workspace_network_name(self, workspace_id: str) -> str:
        return f"opencuria-ws-{workspace_id}"


class WorkspaceServiceDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime = DummyRuntime()
        self.service = WorkspaceService(
            runtimes={"docker": self.runtime},
            settings=RunnerSettings(),
        )
        self.workspace_id = uuid.uuid4()
        self.service._cache[self.workspace_id] = WorkspaceInfo(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
            status="running",
            runtime_type="docker",
        )

    async def test_start_desktop_restarts_stale_cached_session(self) -> None:
        self.service._desktop_sessions[self.workspace_id] = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
        )
        self.runtime.exec_command_wait.side_effect = [
            (1, "dead"),
            (0, "started"),
        ]

        session = await self.service.start_desktop(self.workspace_id)

        self.assertEqual(session.workspace_id, self.workspace_id)
        self.assertEqual(self.runtime.exec_command_wait.await_count, 2)
        self.assertIs(self.service._desktop_sessions[self.workspace_id], session)

    async def test_heartbeat_payload_prunes_stale_desktop_sessions(self) -> None:
        self.service._desktop_sessions[self.workspace_id] = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
        )
        self.runtime.exec_command_wait.return_value = (1, "dead")

        payload = await self.service.get_workspace_heartbeat_statuses()

        self.assertEqual(
            payload,
            [
                {
                    "workspace_id": str(self.workspace_id),
                    "status": "running",
                    "runtime_type": "docker",
                    "desktop": None,
                }
            ],
        )
        self.assertNotIn(self.workspace_id, self.service._desktop_sessions)

    async def test_recover_desktop_sessions_from_runtime_rebuilds_cache(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")

        await self.service.recover_desktop_sessions_from_runtime()

        session = self.service._desktop_sessions[self.workspace_id]
        self.assertEqual(session.workspace_id, self.workspace_id)

    async def test_start_desktop_recovers_live_session_missing_from_cache(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")

        session = await self.service.start_desktop(self.workspace_id)

        self.assertEqual(session.workspace_id, self.workspace_id)
        self.assertEqual(self.runtime.exec_command_wait.await_count, 1)
        self.assertTrue(session.viewer_held)

    async def test_viewer_stop_keeps_process_when_computer_use_holds(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.acquire_desktop(
            self.workspace_id, holder="viewer"
        )
        await self.service.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.runtime.exec_command_wait.reset_mock()
        self.runtime.exec_command_wait.return_value = (0, "alive")

        result = await self.service.stop_desktop(self.workspace_id)

        self.assertFalse(result.stopped)
        self.assertTrue(result.process_alive)
        self.assertTrue(result.computer_use_active)
        self.assertFalse(result.viewer_held)
        self.assertIn(self.workspace_id, self.service._desktop_sessions)
        stop_calls = [
            call
            for call in self.runtime.exec_command_wait.await_args_list
            if call.args[1] == ["/usr/local/bin/opencuria-desktop-stop"]
        ]
        self.assertEqual(stop_calls, [])

    async def test_computer_use_release_stops_process_without_viewer(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.runtime.exec_command_wait.reset_mock()
        self.runtime.exec_command_wait.return_value = (0, "")

        result = await self.service.release_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        self.assertTrue(result.stopped)
        self.assertFalse(result.process_alive)
        self.assertNotIn(self.workspace_id, self.service._desktop_sessions)
        self.assertEqual(
            self.runtime.exec_command_wait.await_args.args[1],
            ["/usr/local/bin/opencuria-desktop-stop"],
        )

    async def test_computer_use_release_keeps_process_with_viewer(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.acquire_desktop(
            self.workspace_id, holder="viewer"
        )
        await self.service.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        result = await self.service.release_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        self.assertFalse(result.stopped)
        self.assertTrue(result.process_alive)
        self.assertTrue(result.viewer_held)
        self.assertIn(self.workspace_id, self.service._desktop_sessions)

    async def test_parallel_computer_use_runs_stop_after_last_release(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-a"
        )
        await self.service.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-b"
        )

        first = await self.service.release_desktop(
            self.workspace_id, holder="computeruse", run_id="run-a"
        )
        self.assertFalse(first.stopped)
        self.assertTrue(first.computer_use_active)

        self.runtime.exec_command_wait.return_value = (0, "")
        second = await self.service.release_desktop(
            self.workspace_id, holder="computeruse", run_id="run-b"
        )
        self.assertTrue(second.stopped)
        self.assertNotIn(self.workspace_id, self.service._desktop_sessions)

    async def test_force_stop_kills_process_and_recordings(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.acquire_desktop(
            self.workspace_id, holder="viewer"
        )
        await self.service.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.service._desktop_recordings[(self.workspace_id, "run-1")] = (
            4242,
            "/workspace/.opencuria/computeruse/run-1/session.mp4",
        )
        self.runtime.exec_command_wait.reset_mock()
        self.runtime.exec_command_wait.return_value = (0, "")

        result = await self.service.release_desktop(
            self.workspace_id, holder="viewer", force=True
        )

        self.assertTrue(result.stopped)
        self.assertNotIn(self.workspace_id, self.service._desktop_sessions)
        self.assertEqual(self.service._desktop_recordings, {})
        commands = [
            call.args[1] for call in self.runtime.exec_command_wait.await_args_list
        ]
        self.assertTrue(
            any(
                isinstance(cmd, list)
                and len(cmd) == 3
                and "kill -INT 4242" in cmd[2]
                for cmd in commands
            )
        )
        self.assertIn(
            ["/usr/local/bin/opencuria-desktop-stop"],
            commands,
        )

    async def test_ensure_does_not_acquire_viewer_lease(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")

        session = await self.service.ensure_desktop_process(self.workspace_id)

        self.assertFalse(session.viewer_held)
        self.assertEqual(session.computeruse_run_ids, set())

    async def test_heartbeat_includes_lease_flags(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        payload = await self.service.get_workspace_heartbeat_statuses()

        desktop = payload[0]["desktop"]
        self.assertEqual(desktop["port"], 6901)
        self.assertFalse(desktop["viewer"])
        self.assertTrue(desktop["computer_use"])
