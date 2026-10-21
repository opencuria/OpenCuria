import asyncio
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.models import DesktopSession, WorkspaceInfo
from src.service import WorkspaceService
from src.services.sessions.desktop import DESKTOP_STOP_COMMAND
from src.services.sessions.desktop_leases import DesktopLeaseStore
from src.services.sessions.stream_intents import StreamIntentStore


class DummyRuntime:
    def __init__(self) -> None:
        self.desktop_live = False
        self.exec_calls: list[tuple[str, list[str], dict]] = []
        self.exec_command_wait = AsyncMock(side_effect=self._exec_command_wait)
        self.remove_workspace = AsyncMock(side_effect=self._remove_workspace)
        self.workspace_status = "running"
        self.removal_started: asyncio.Event | None = None
        self.finish_removal: asyncio.Event | None = None

    async def _exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        **kwargs,
    ) -> tuple[int, str]:
        self.exec_calls.append((instance_id, list(command), kwargs))
        text = " ".join(command)
        if "connect_ex" in text:
            return (0, "alive") if self.desktop_live else (1, "dead")
        if command == ["bash", "-lc", DESKTOP_STOP_COMMAND]:
            self.desktop_live = False
            return 0, "stopped"
        if "/usr/bin/Xvnc :1 -geometry" in text:
            if self.removal_started is not None:
                self.removal_started.set()
            if self.finish_removal is not None:
                await asyncio.wait_for(self.finish_removal.wait(), timeout=5)
            self.desktop_live = True
            return 0, "started"
        return 0, "ok"

    async def _remove_workspace(self, instance_id: str) -> None:
        if self.removal_started is not None:
            self.removal_started.set()
        if self.finish_removal is not None:
            await asyncio.wait_for(self.finish_removal.wait(), timeout=5)
        self.workspace_status = "removed"

    async def get_workspace_status(self, instance_id: str) -> SimpleNamespace:
        return SimpleNamespace(instance_id=instance_id, status=self.workspace_status)

    async def workspace_exists(self, instance_id: str) -> bool:
        return self.workspace_status != "removed"

    def get_container_ip(self, instance_id: str, workspace_id: str) -> str:
        return "172.22.0.2"

    def get_workspace_network_name(self, workspace_id: str) -> str:
        return f"opencuria-ws-{workspace_id}"


class WorkspaceServiceDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = DummyRuntime()
        self.service = WorkspaceService(
            runtimes={"docker": self.runtime},
            settings=RunnerSettings(state_dir=self.directory.name),
        )
        self.workspace_id = uuid.uuid4()
        self.service.registry._cache[self.workspace_id] = WorkspaceInfo(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
            status="running",
            runtime_type="docker",
        )
        self.epoch = "desktop-test-epoch"
        self.lease_store = DesktopLeaseStore(self.directory.name)
        self.close_owner_streams = AsyncMock(return_value=True)
        self.service.desktop.configure_leases(
            self.lease_store, self.epoch, self.close_owner_streams
        )

    def intent(
        self,
        lease_id: str,
        *,
        kind: str = "viewer",
        owner_id: str | None = None,
        revision: int = 1,
    ) -> dict:
        return {
            "lease_id": lease_id,
            "kind": kind,
            "owner_id": owner_id or lease_id,
            "epoch": self.epoch,
            "revision": revision,
        }

    async def reserve(self, intent: dict) -> None:
        await self.service.desktop.desktop_action(self.workspace_id, "reserve", intent)

    async def hold(self, intent: dict) -> dict:
        await self.reserve(intent)
        return await self.service.desktop.desktop_action(
            self.workspace_id, "hold", intent
        )

    async def test_hold_starts_xvnc_without_remote_resize(self) -> None:
        """A fresh display uses fixed geometry and disables remote resize."""
        await self.hold(
            {
                **self.intent("viewer-lease"),
                "desktop_width": 1280,
                "desktop_height": 720,
            }
        )

        commands = [command for _, command, _ in self.runtime.exec_calls]
        start_command = next(
            command
            for command in commands
            if len(command) > 2 and "-geometry 1280x720" in command[2]
        )
        self.assertIn("-geometry 1280x720", start_command[2])
        self.assertIn("-AcceptSetDesktopSize=0", start_command[2])
        self.assertNotIn("opencuria-desktop-start", start_command[2])
        self.assertNotIn("opencuria-desktop-stop", start_command[2])
        self.assertIn(["bash", "-lc", DESKTOP_STOP_COMMAND], commands)
        self.assertFalse(any("opencuria-desktop-stop" in str(c) for c in commands))

    async def test_start_command_waits_for_x11_socket_and_kasm_port(self) -> None:
        command = self.service.desktop._desktop_start_command(1920, 1080)
        self.assertIn("/tmp/.X11-unix/X1", command)
        self.assertIn("/dev/tcp/127.0.0.1/6901", command)
        self.assertIn(".xstartup-started", command)
        self.assertEqual(command.count("/root/.vnc/xstartup >>"), 1)
        marker_clear = command.index("rm -f /root/.vnc/.xstartup-started")
        xvnc_launch = command.index("/usr/bin/Xvnc :1")
        xstartup_launch = command.index("/root/.vnc/xstartup >>")
        self.assertLess(marker_clear, xvnc_launch)
        self.assertLess(xvnc_launch, xstartup_launch)

    async def test_start_command_creates_xauthority_for_xlib_clients(self) -> None:
        command = self.service.desktop._desktop_start_command(1920, 1080)
        self.assertIn("touch /root/.Xauthority", command)
        self.assertLess(
            command.index("touch /root/.Xauthority"),
            command.index("/usr/bin/Xvnc :1"),
        )

    async def test_concurrent_managed_holds_start_xvnc_once(self) -> None:
        """Viewer and computer-use owners share one in-flight display start."""
        entered_start = asyncio.Event()
        release_start = asyncio.Event()

        async def exec_command_wait(instance_id, command, **kwargs):
            text = command[2] if len(command) > 2 else " ".join(command)
            if "connect_ex" in text:
                return (0, "alive") if self.runtime.desktop_live else (1, "dead")
            if command == ["bash", "-lc", DESKTOP_STOP_COMMAND]:
                self.runtime.desktop_live = False
                return 0, "stopped"
            if "/usr/bin/Xvnc :1 -geometry" in text:
                entered_start.set()
                await asyncio.wait_for(release_start.wait(), timeout=5)
                self.runtime.desktop_live = True
                return 0, "started"
            return 0, "ok"

        self.runtime.exec_command_wait.side_effect = exec_command_wait
        viewer = self.intent("viewer-lease", kind="viewer")
        computeruse = self.intent(
            "computeruse-lease", kind="computeruse", owner_id="run-1"
        )
        await self.reserve(viewer)
        viewer_task = asyncio.create_task(
            self.service.desktop.desktop_action(self.workspace_id, "hold", viewer)
        )
        await asyncio.wait_for(entered_start.wait(), timeout=5)

        computeruse_task = asyncio.create_task(self.hold(computeruse))
        await asyncio.sleep(0.05)
        self.assertFalse(computeruse_task.done())
        release_start.set()
        viewer_result, computeruse_result = await asyncio.gather(
            viewer_task, computeruse_task
        )

        session = self.service.desktop._desktop_sessions[self.workspace_id]
        self.assertTrue(viewer_result["ok"])
        self.assertTrue(computeruse_result["ok"])
        self.assertTrue(session.viewer_held)
        self.assertIn("run-1", session.computeruse_run_ids)
        self.assertEqual((await self.lease_store.get("viewer-lease"))["state"], "held")
        self.assertEqual(
            (await self.lease_store.get("computeruse-lease"))["state"], "held"
        )
        self.assertEqual(
            sum(
                1
                for call in self.runtime.exec_command_wait.await_args_list
                if "/usr/bin/Xvnc :1 -geometry" in call.args[1][2]
            ),
            1,
        )

    async def test_release_only_ends_the_matching_managed_owner(self) -> None:
        viewer = self.intent("viewer-lease")
        computeruse = self.intent(
            "computeruse-lease", kind="computeruse", owner_id="run-1"
        )
        await self.hold(viewer)
        await self.hold(computeruse)
        stop_calls = sum(
            command == ["bash", "-lc", DESKTOP_STOP_COMMAND]
            for _, command, _ in self.runtime.exec_calls
        )

        result = await self.service.desktop.desktop_action(
            self.workspace_id, "release", viewer
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["lease_state"], "released")
        self.assertEqual(
            (await self.lease_store.get("computeruse-lease"))["state"], "held"
        )
        self.assertTrue(
            self.service.desktop._desktop_sessions[
                self.workspace_id
            ].computeruse_run_ids
        )
        self.close_owner_streams.assert_awaited_once_with("viewer-lease")
        self.assertEqual(
            stop_calls,
            sum(
                command == ["bash", "-lc", DESKTOP_STOP_COMMAND]
                for _, command, _ in self.runtime.exec_calls
            ),
        )

    async def test_heartbeat_stale_negative_does_not_prune_replacement(self) -> None:
        await self.hold(self.intent("viewer-old"))
        old_session = self.service.desktop._desktop_sessions[self.workspace_id]
        initial_probe_started = asyncio.Event()
        finish_initial_probe = asyncio.Event()
        probe_count = 0

        async def heartbeat_probe(workspace_id: uuid.UUID) -> bool:
            nonlocal probe_count
            self.assertEqual(workspace_id, self.workspace_id)
            probe_count += 1
            if probe_count == 1:
                initial_probe_started.set()
                await finish_initial_probe.wait()
                return False
            return True

        self.service.registry.desktop_live = heartbeat_probe
        self.service.desktop._is_desktop_session_live = AsyncMock(
            side_effect=[False, True]
        )
        heartbeat = asyncio.create_task(
            self.service.registry.get_workspace_heartbeat_statuses()
        )
        await asyncio.wait_for(initial_probe_started.wait(), timeout=1)

        await self.hold(self.intent("viewer-new", owner_id="new-owner"))
        replacement = self.service.desktop._desktop_sessions[self.workspace_id]
        self.assertIsNot(replacement, old_session)
        finish_initial_probe.set()
        payload = await heartbeat

        self.assertIs(
            self.service.desktop._desktop_sessions[self.workspace_id], replacement
        )
        self.assertIsNotNone(payload[0]["desktop"])
        self.assertTrue(payload[0]["desktop_session_tracked"])
        self.assertTrue(payload[0]["desktop"]["viewer"])
        self.assertEqual(probe_count, 2)

    async def test_heartbeat_probe_exception_preserves_cached_session(self) -> None:
        await self.hold(self.intent("viewer-lease"))
        session = self.service.desktop._desktop_sessions[self.workspace_id]
        probes = AsyncMock(side_effect=RuntimeError("transient runtime error"))
        self.service.registry.desktop_live = probes

        payload = await self.service.registry.get_workspace_heartbeat_statuses()

        self.assertIs(
            self.service.desktop._desktop_sessions[self.workspace_id], session
        )
        self.assertTrue(payload[0]["desktop_session_tracked"])
        self.assertIsNone(payload[0]["desktop"])
        probes.assert_awaited_once_with(self.workspace_id)

    async def test_heartbeat_confirmed_stop_prunes_stale_session(self) -> None:
        await self.hold(self.intent("viewer-lease"))
        generation = self.service.desktop.get_desktop_generation(self.workspace_id)
        self.service.registry.desktop_live = AsyncMock(return_value=False)

        payload = await self.service.registry.get_workspace_heartbeat_statuses()

        self.assertIsNone(payload[0]["desktop"])
        self.assertFalse(payload[0]["desktop_session_tracked"])
        self.assertEqual(payload[0]["desktop_generation"], generation)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        self.assertEqual(self.service.registry.desktop_live.await_count, 2)

    async def test_recovery_repairs_leases_without_pruning_cached_session(self) -> None:
        session = DesktopSession(self.workspace_id, "instance-1", generation=5)
        self.service.desktop._desktop_sessions[self.workspace_id] = session
        self.service.desktop._desktop_generations[self.workspace_id] = 5
        self.runtime.exec_command_wait.reset_mock()

        await self.service.registry.recover_desktop_sessions_from_runtime()

        self.assertIs(
            self.service.desktop._desktop_sessions[self.workspace_id], session
        )
        self.assertEqual(
            self.service.desktop.get_desktop_generation(self.workspace_id), 5
        )
        self.runtime.exec_command_wait.assert_not_awaited()

    async def test_stale_session_restart_increments_generation(self) -> None:
        stale = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
            generation=3,
        )
        self.service.desktop._desktop_sessions[self.workspace_id] = stale

        await self.hold(self.intent("viewer-lease"))
        session = self.service.desktop.get_desktop_session(self.workspace_id)

        self.assertIsNot(session, stale)
        self.assertEqual(session.generation, 4)
        self.assertIs(
            self.service.desktop._desktop_sessions[self.workspace_id], session
        )

    async def test_remove_waits_for_physical_disposal_before_forgetting_workspace(
        self,
    ) -> None:
        self.service.streams.close_workspace_streams = AsyncMock(return_value=0)
        self.service.streams.intent_store = StreamIntentStore(self.directory.name)
        self.service.streams.lease_store = self.lease_store
        self.service.streams.epoch = self.epoch
        self.service.background._kill_all_background_processes = AsyncMock()
        self.close_owner_streams.return_value = False
        confirmed = AsyncMock(wraps=self.service.streams.confirm_workspace_ended)
        self.service.streams.confirm_workspace_ended = confirmed
        owner = self.intent("remove-owner")
        await self.hold(owner)
        self.runtime.removal_started = asyncio.Event()
        self.runtime.finish_removal = asyncio.Event()

        removal = asyncio.create_task(self.service.remove_workspace(self.workspace_id))
        await asyncio.wait_for(self.runtime.removal_started.wait(), timeout=1)
        self.assertIn(self.workspace_id, self.service.registry._cache)
        self.assertEqual(
            (await self.lease_store.get("remove-owner"))["state"], "closing"
        )

        self.runtime.finish_removal.set()
        await asyncio.wait_for(removal, timeout=5)

        confirmed.assert_awaited_once_with(self.workspace_id, "instance-1")
        self.assertNotIn(self.workspace_id, self.service.registry._cache)
        self.assertEqual(self.runtime.workspace_status, "removed")
        self.assertEqual(
            (await self.lease_store.get("remove-owner"))["state"], "expired"
        )

    async def test_remove_requires_physical_registry_identity(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "Managed workspace removal requires physical registry identity",
        ):
            await self.service.remove_workspace(uuid.uuid4())
        self.runtime.remove_workspace.assert_not_awaited()

    async def test_failed_physical_confirmation_keeps_registry_identity(self) -> None:
        self.service.streams.close_workspace_streams = AsyncMock(return_value=0)
        self.service.background._kill_all_background_processes = AsyncMock()
        self.service.streams.confirm_workspace_ended = AsyncMock(return_value=False)

        with self.assertRaisesRegex(
            RuntimeError, "physical disposal confirmation failed"
        ):
            await self.service.remove_workspace(self.workspace_id)

        self.assertIn(self.workspace_id, self.service.registry._cache)
        self.runtime.remove_workspace.assert_awaited_once_with("instance-1")
