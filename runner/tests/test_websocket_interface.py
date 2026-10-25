import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from src.config import RunnerSettings
from src.interfaces.websocket import SOCKETIO_MAX_HTTP_BUFFER_SIZE, WebSocketInterface
from src.models import DesktopSession, WorkspaceInfo
from src.runtime.base import RuntimeStatus, RuntimeWorkspaceInfo
from src.service import WorkspaceService
from src.services.sessions.desktop import DESKTOP_STOP_COMMAND


class DummyService:
    def __init__(self) -> None:
        self.registry = SimpleNamespace(_cache={})
        self.sync_from_runtime = AsyncMock()
        self.recover_desktop_sessions_from_runtime = AsyncMock()
        self.run_health_check_loop = AsyncMock()
        self.get_workspace_heartbeat_statuses = AsyncMock(return_value=[])
        self.create_workspace_calls = []
        # WebSocket terminal and image handlers call the managers directly.
        self.terminal_manager = AsyncMock()
        self.images = AsyncMock()
        self.images.create_workspace_from_image_artifact = AsyncMock(
            side_effect=self._create_workspace_from_image_artifact
        )
        # WebSocket desktop handlers use the manager directly, as the real
        # WorkspaceService composition does.
        self.desktop = self._FakeDesktop()

    class _FakeDesktop:
        def __init__(self) -> None:
            self.epoch = "runner-desktop-epoch"
            self.sessions = {}
            self.desktop_action = AsyncMock(side_effect=self._desktop_action)

        async def _desktop_action(self, workspace_id, action, args=None):
            args = args or {}
            session = self.sessions.get(workspace_id)
            generation = getattr(session, "generation", 0)
            if action == "hold":
                generation += 1
                session = SimpleNamespace(
                    port=6901,
                    viewer_held=args.get("kind") == "viewer",
                    computeruse_run_ids=set(),
                    generation=generation,
                )
                self.sessions[workspace_id] = session
                return {
                    "ok": True,
                    "port": session.port,
                    "viewer": session.viewer_held,
                    "computer_use": False,
                    "generation": generation,
                    "epoch": self.epoch,
                }
            if action == "release":
                if session is not None and args.get("kind") == "viewer":
                    session.viewer_held = False
                process_alive = bool(
                    session
                    and (session.viewer_held or session.computeruse_run_ids)
                )
                if not process_alive:
                    self.sessions.pop(workspace_id, None)
                return {
                    "ok": True,
                    "stopped": not process_alive,
                    "process_alive": process_alive,
                    "viewer_held": bool(session and session.viewer_held),
                    "computer_use_active": bool(
                        session and session.computeruse_run_ids
                    ),
                    "generation": generation,
                    "epoch": self.epoch,
                }
            raise AssertionError(f"Unexpected desktop action in task handler: {action}")

        def get_desktop_generation(self, workspace_id):
            return getattr(self.get_desktop_session(workspace_id), "generation", 0)

        def get_desktop_guest_ip(self, workspace_id):
            return "127.0.0.1"

        def get_desktop_network_name(self, workspace_id):
            return "workspace-net"

        def get_desktop_session(self, workspace_id):
            return self.sessions.get(workspace_id)

        def get_desktop_state_payload(self, workspace_id):
            session = self.get_desktop_session(workspace_id)
            if session is None:
                return None
            return {
                "workspace_id": str(workspace_id),
                "port": session.port,
                "guest_ip": self.get_desktop_guest_ip(workspace_id),
                "viewer": session.viewer_held,
                "computer_use": bool(session.computeruse_run_ids),
                "generation": session.generation,
                "epoch": self.epoch,
            }

    async def _create_workspace_from_image_artifact(self, **kwargs):
        return kwargs["new_workspace_id"], bool(
            kwargs.get("env_vars") or kwargs.get("ssh_keys") or kwargs.get("files")
        )

    async def create_workspace(self, **kwargs):
        self.create_workspace_calls.append(kwargs)
        return kwargs.get("workspace_id") or uuid.uuid4(), bool(
            kwargs.get("env_vars") or kwargs.get("ssh_keys") or kwargs.get("files")
        )

    async def inject_credentials(self, workspace_id, **kwargs):
        return bool(
            kwargs.get("env_vars") or kwargs.get("ssh_keys") or kwargs.get("files")
        )


class WebSocketHandlerRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_task_handlers_are_not_registered(self) -> None:
        interface = WebSocketInterface(
            DummyService(), RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        assert {
            "task:run_prompt",
            "task:cancel_prompt",
            "task:cleanup_unknown_workspace",
        }.isdisjoint(interface._sio.handlers["/"])


class WebSocketVmMetricsPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.interface = WebSocketInterface(
            DummyService(), RunnerSettings(state_dir=tempfile.mkdtemp())
        )

    def test_forwards_guest_disk_counters_when_reported(self) -> None:
        payload = self.interface._build_vm_metrics_payload(
            {
                "ws-1": {
                    "cpu_time_ns": 0,
                    "vcpu_count": 2,
                    "disk_used_bytes": 3,
                    "disk_total_bytes": 4,
                    "guest_disk_used_bytes": 10,
                    "guest_disk_free_bytes": 20,
                    "guest_disk_total_bytes": 31,
                }
            }
        )

        assert payload["ws-1"]["guest_disk_used_bytes"] == 10
        assert payload["ws-1"]["guest_disk_free_bytes"] == 20
        assert payload["ws-1"]["guest_disk_total_bytes"] == 31
        assert payload["ws-1"]["disk_used_bytes"] == 3

    def test_omits_guest_disk_counters_when_guest_did_not_answer(self) -> None:
        payload = self.interface._build_vm_metrics_payload(
            {"ws-1": {"disk_used_bytes": 3, "disk_total_bytes": 0}}
        )

        assert "guest_disk_used_bytes" not in payload["ws-1"]
        assert payload["ws-1"]["disk_used_bytes"] == 3


class WebSocketReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_loop_survives_handshake_timeouts(self) -> None:
        """A hanging backend must not kill the reconnect loop (and the runner)."""
        interface = WebSocketInterface(
            DummyService(), RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        sio = interface._sio
        sio.reconnection_delay = sio.reconnection_delay_max = 0.01
        sio.randomization_factor = 0
        attempts = 0

        async def failing_connect(*args, **kwargs) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                # aiohttp's handshake timeout, raised when the backend
                # accepts TCP but never answers (e.g. during a restart).
                raise asyncio.TimeoutError()
            sio._reconnect_abort.set()
            raise aiohttp.ClientConnectionError("refused")

        sio.eio.connect = failing_connect
        await asyncio.wait_for(sio._handle_reconnect(), 5)

        self.assertEqual(attempts, 3)


class WebSocketMetricsPathTests(unittest.TestCase):
    def test_socketio_client_raises_http_buffer_size(self) -> None:
        """aiohttp inbound WS messages must accept 200 MiB like the backend."""
        interface = WebSocketInterface(
            DummyService(), RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        self.assertEqual(SOCKETIO_MAX_HTTP_BUFFER_SIZE, 200 * 1024 * 1024)
        self.assertEqual(
            interface._sio.eio.websocket_extra_options["max_msg_size"],
            SOCKETIO_MAX_HTTP_BUFFER_SIZE,
        )

    def test_storage_root_defaults_to_var_lib_opencuria(self) -> None:
        settings = RunnerSettings(
            state_dir=tempfile.mkdtemp(),
            qemu_image_cache_dir="/var/lib/opencuria/images",
            qemu_disk_dir="/var/lib/opencuria/disks",
            qemu_snapshot_dir="/var/lib/opencuria/snapshots",
        )
        interface = WebSocketInterface(DummyService(), settings)
        self.assertEqual(
            interface._storage_root_path().as_posix(), "/var/lib/opencuria"
        )

    def test_storage_root_respects_custom_common_base(self) -> None:
        settings = RunnerSettings(
            state_dir=tempfile.mkdtemp(),
            qemu_image_cache_dir="/mnt/kern-store/images",
            qemu_disk_dir="/mnt/kern-store/disks",
            qemu_snapshot_dir="/mnt/kern-store/snapshots",
        )
        interface = WebSocketInterface(DummyService(), settings)
        self.assertEqual(interface._storage_root_path().as_posix(), "/mnt/kern-store")

    def test_resolve_disk_usage_path_falls_back_to_existing_parent(self) -> None:
        settings = RunnerSettings(
            state_dir=tempfile.mkdtemp(),
            qemu_image_cache_dir="/tmp/runner-metrics-test/images",
            qemu_disk_dir="/tmp/runner-metrics-test/disks",
            qemu_snapshot_dir="/tmp/runner-metrics-test/snapshots",
        )
        interface = WebSocketInterface(DummyService(), settings)
        self.assertEqual(interface._resolve_disk_usage_path(), "/tmp")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["removed", "replaced"])
async def test_observation_tolerates_workspace_changes_during_probe(
    tmp_path: Path, change: str
) -> None:
    """Lifecycle changes must not abort heartbeats or update stale cache objects."""
    service = DummyService()
    workspace_id = uuid.uuid4()
    other_id = uuid.uuid4()
    previous = WorkspaceInfo(workspace_id, "old-guest", "running")
    service.registry._cache = {
        workspace_id: previous,
        other_id: WorkspaceInfo(other_id, "other-guest", "running"),
    }
    entered = asyncio.Event()
    release = asyncio.Event()
    heartbeat = asyncio.Event()
    identity = {"domain_uuid": "old-guest"}

    async def incarnation(observed_id: str) -> tuple[dict, str] | None:
        if observed_id == str(workspace_id):
            entered.set()
            await release.wait()
            return identity, "running"
        return None

    async def statuses() -> list[dict]:
        heartbeat.set()
        return []

    service.workspace_incarnation = AsyncMock(side_effect=incarnation)
    service.get_workspace_heartbeat_statuses.side_effect = statuses
    interface = WebSocketInterface(
        service,
        RunnerSettings(state_dir=str(tmp_path), inventory_interval=10**12),
    )
    interface._operations.journal.checkpoint(
        str(workspace_id),
        identity,
        {"state": "running", "credentials_present": True},
    )
    observation = asyncio.create_task(interface._observation_loop())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if change == "removed":
            service.registry._cache.pop(workspace_id)
        else:
            service.registry._cache[workspace_id] = WorkspaceInfo(
                workspace_id, "new-guest", "running"
            )
        release.set()
        await asyncio.wait_for(heartbeat.wait(), timeout=2)
        assert previous.credentials_present is None
        if change == "replaced":
            assert service.registry._cache[workspace_id].credentials_present is None
    finally:
        observation.cancel()
        await asyncio.gather(observation, return_exceptions=True)
        interface._operations.journal.close()


class WebSocketDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_announces_desktop_ended_by_maintenance(self) -> None:
        """Observe maintenance stop using the canonical registry cache."""
        with tempfile.TemporaryDirectory() as state_dir:
            workspace_id = uuid.uuid4()
            maintenance_started = asyncio.Event()

            class Runtime:
                async def get_workspace_status(self, instance_id):
                    return RuntimeStatus(instance_id, "running")

                async def workspace_incarnation(self, _workspace_id):
                    return (
                        {"runtime": "qemu", "resource_id": "instance-1"},
                        "running",
                    )

                async def list_workspaces(self):
                    return [
                        RuntimeWorkspaceInfo(
                            str(workspace_id), "instance-1", "running"
                        )
                    ]

                def get_guest_ip(self, instance_id, workspace_id):
                    return "127.0.0.1"

                def get_workspace_network_name(self, workspace_id):
                    return "workspace-net"

                async def exec_command_wait(self, instance_id, command, **kwargs):
                    if command == ["bash", "-lc", DESKTOP_STOP_COMMAND]:
                        maintenance_started.set()
                        return 0, ""
                    if command[0:2] == ["sh", "-lc"]:
                        return 0, "stopped"
                    return 0, ""

            service = WorkspaceService(Runtime(), RunnerSettings(state_dir=state_dir))
            service.registry._cache[workspace_id] = WorkspaceInfo(
                workspace_id, "instance-1", "running"
            )
            self.assertFalse(hasattr(service, "_cache"))
            interface = WebSocketInterface(
                service,
                RunnerSettings(state_dir=state_dir, inventory_interval=10**12),
            )
            interface._sio.connected = True
            interface._operations.replay = AsyncMock()
            emitted = asyncio.Event()

            async def emit(event, payload):
                if event == "desktop:stopped":
                    self.assertEqual(payload["workspace_id"], str(workspace_id))
                    emitted.set()

            interface._sio.emit = AsyncMock(side_effect=emit)
            session = DesktopSession(workspace_id, "instance-1", generation=4)
            service.desktop._desktop_sessions[workspace_id] = session
            service.desktop._desktop_generations[workspace_id] = 4
            await service.desktop.lease_store.reserve(
                str(workspace_id),
                "instance-1",
                "expired-viewer-lease",
                "viewer",
                "viewer-owner",
                service.desktop.epoch,
                1,
                180,
            )
            await service.desktop.lease_store.activate(
                "expired-viewer-lease", service.desktop.epoch, 1
            )
            # Persist an already-expired owner so real maintenance selects
            # the workspace immediately, without waiting on lease wall-clock TTL.
            lease_path = service.desktop.lease_store.path

            def expire_lease() -> None:
                with sqlite3.connect(lease_path) as db:
                    db.execute(
                        "UPDATE desktop_leases SET expires_at=0 "
                        "WHERE lease_id='expired-viewer-lease'"
                    )

            await asyncio.to_thread(expire_lease)
            # The real manager maintenance cleans the expired owner and stops
            # the desktop; this event marks its stop exec before the event is
            # announced by the observation loop.
            observation = asyncio.create_task(interface._observation_loop())
            try:
                await asyncio.wait_for(maintenance_started.wait(), timeout=2)
                await asyncio.wait_for(emitted.wait(), timeout=2)
            finally:
                observation.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await observation
                interface._operations.journal.close()

            interface._sio.emit.assert_any_await(
                "desktop:stopped",
                {
                    "workspace_id": str(workspace_id),
                    "stopped": True,
                    "generation": 4,
                    "epoch": interface._desktop_epoch,
                },
            )
            self.assertNotIn(workspace_id, service.desktop._desktop_sessions)
            lease = await service.desktop.lease_store.get("expired-viewer-lease")
            self.assertEqual(lease["state"], "expired")

    async def test_connect_snapshot_is_reused_for_first_heartbeat(self) -> None:
        """Connect snapshot is not immediately re-probed for the first heartbeat."""
        service = DummyService()
        workspaces = [{"workspace_id": str(uuid.uuid4()), "status": "running"}]
        service.get_workspace_heartbeat_statuses = AsyncMock(return_value=workspaces)
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.connected = True
        emitted = asyncio.Event()

        async def _emit(event, payload):
            if event == "runner:heartbeat":
                emitted.set()

        interface._sio.emit = AsyncMock(side_effect=_emit)
        heartbeat = asyncio.create_task(
            interface._heartbeat_loop(initial_workspaces=workspaces)
        )
        await asyncio.wait_for(emitted.wait(), timeout=1)
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat

        service.sync_from_runtime.assert_not_awaited()
        service.get_workspace_heartbeat_statuses.assert_not_awaited()

    async def test_connect_recovers_desktop_sessions_before_reannounce(self) -> None:
        service = DummyService()
        service.get_workspace_heartbeat_statuses = AsyncMock(
            return_value=[
                {
                    "workspace_id": str(uuid.uuid4()),
                    "status": "running",
                    "desktop": {
                        "port": 6901,
                        "guest_ip": "127.0.0.1",
                    },
                }
            ]
        )

        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()

        await interface._sio.handlers["/"]["connect"]()

        # Registration must finish before slow observation starts.
        service.sync_from_runtime.assert_not_awaited()
        interface._sio.emit.assert_any_await(
            "runner:register",
            {
                "status": "ready",
                "inventory_epoch": interface._inventory_epoch,
                "desktop_leases": {
                    "protocol_version": 1,
                    "epoch": interface._desktop_epoch,
                },
            },
        )
        await interface.stop()

    async def test_blocked_inventory_does_not_block_health_or_operation_lease(self):
        import asyncio
        from unittest.mock import patch

        # Replace only this module's clock reference, not asyncio's clock.
        fresh_host_clock = patch(
            "src.interfaces.websocket.time", SimpleNamespace(monotonic=lambda: 1.0)
        )
        fresh_host_clock.start()
        self.addCleanup(fresh_host_clock.stop)
        service = DummyService()
        blocked = asyncio.Event()
        entered = asyncio.Event()

        async def scan():
            entered.set()
            await blocked.wait()

        service.inventory_for_runner = AsyncMock(side_effect=scan)
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp(), inventory_interval=0)
        )
        interface._sio.connected = True
        emitted = asyncio.Event()

        async def emit(event, data):
            if (
                event == "runner:heartbeat"
                and data.get("workspace_states_observed") is False
            ):
                assert data["snapshot_complete"] is False
                emitted.set()

        interface._sio.emit = AsyncMock(side_effect=emit)
        interface._operations.replay = AsyncMock()
        observation = asyncio.create_task(interface._observation_loop())
        heartbeat = None
        try:
            # Explicit inventory_interval=0 forces the scan on the first tick,
            # even on a freshly booted CI host whose monotonic uptime is <120s.
            # Keep initialization bounded without assuming host performance.
            await asyncio.wait_for(entered.wait(), 10)
            heartbeat = asyncio.create_task(interface._heartbeat_loop())
            await asyncio.wait_for(emitted.wait(), 10)
            interface._operations.replay.assert_any_await(recover=False)
            assert not blocked.is_set()
            assert not observation.done()
        finally:
            tasks = [task for task in (heartbeat, observation) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _viewer_lease(workspace_id: uuid.UUID, *, revision: int = 1) -> dict:
        return {
            "lease_id": f"viewer-{workspace_id}",
            "kind": "viewer",
            "owner_id": "viewer-owner",
            "revision": revision,
            "epoch": "runner-desktop-epoch",
        }

    async def test_start_desktop_emits_qemu_proxy_metadata(self) -> None:
        service = DummyService()
        service.desktop.get_desktop_guest_ip = lambda workspace_id: "10.100.0.2"
        service.desktop.get_desktop_network_name = lambda workspace_id: ""
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()

        task_id = "desktop-task-1"
        workspace_id = uuid.uuid4()
        lease = self._viewer_lease(workspace_id)
        handler = interface._sio.handlers["/"]["task:start_desktop"]
        await handler(
            {
                "task_id": task_id,
                "workspace_id": str(workspace_id),
                **lease,
                "desktop_width": 1280,
                "desktop_height": 720,
            }
        )

        service.desktop.desktop_action.assert_awaited_once_with(
            workspace_id,
            "hold",
            {**lease, "desktop_width": 1280, "desktop_height": 720},
        )
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertEqual(events, ["desktop:process", "desktop:started"])
        started = interface._sio.emit.await_args_list[-1].args[1]
        self.assertEqual(
            started,
            {
                "workspace_id": str(workspace_id),
                "port": 6901,
                "guest_ip": "10.100.0.2",
                "viewer": True,
                "computer_use": False,
                "generation": 1,
                "epoch": interface._desktop_epoch,
                "ok": True,
                "task_id": task_id,
            },
        )

    async def test_start_uses_current_ownership_after_concurrent_viewer_release(self):
        service = DummyService()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        workspace_id = uuid.uuid4()
        lease = self._viewer_lease(workspace_id)

        async def release_during_announcement(event, payload):
            if event == "desktop:process":
                session = service.desktop.get_desktop_session(workspace_id)
                session.viewer_held = False
                session.computeruse_run_ids = {"active-run"}

        interface._sio.emit = AsyncMock(side_effect=release_during_announcement)
        await interface._sio.handlers["/"]["task:start_desktop"](
            {"task_id": "hold", "workspace_id": str(workspace_id), **lease}
        )
        started = interface._sio.emit.await_args_list[-1].args[1]
        self.assertFalse(started["viewer"])
        self.assertTrue(started["computer_use"])

    async def test_stop_desktop_emits_stopped_when_process_ends(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()
        task_id = "desktop-stop-1"
        workspace_id = uuid.uuid4()
        lease = self._viewer_lease(workspace_id)

        handler = interface._sio.handlers["/"]["task:stop_desktop"]
        await handler(
            {"task_id": task_id, "workspace_id": str(workspace_id), **lease}
        )

        service.desktop.desktop_action.assert_awaited_once_with(
            workspace_id, "release", lease
        )
        events = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:stopped", events)
        self.assertIn("desktop:viewer_released", events)
        stopped = next(
            call.args[1]
            for call in interface._sio.emit.await_args_list
            if call.args[0] == "desktop:stopped"
        )
        self.assertEqual(stopped["workspace_id"], str(workspace_id))
        self.assertEqual(stopped["generation"], 0)
        self.assertEqual(stopped["epoch"], "runner-desktop-epoch")

    async def test_stop_during_replacement_close_announces_current_generation(
        self,
    ) -> None:
        """A stop awaited during tunnel close must not publish stale generation."""
        import asyncio

        service = DummyService()
        workspace_id = uuid.uuid4()
        old = SimpleNamespace(generation=4, viewer_held=True, computeruse_run_ids=set())
        new = SimpleNamespace(generation=5, viewer_held=True, computeruse_run_ids=set())
        current = {"session": None}
        service.desktop.get_desktop_session = lambda _: current["session"]
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        service.desktop.get_desktop_state_payload = lambda ws_id: {
            "workspace_id": str(ws_id),
            "port": 6901,
            "guest_ip": "10.0.0.5",
            "generation": 5,
            "epoch": interface._desktop_epoch,
            "viewer": True,
        }
        interface._sio.emit = AsyncMock()
        close_entered = asyncio.Event()
        release_close = asyncio.Event()

        close_calls = 0

        async def blocked_close(
            _workspace_id,
            *,
            generation=None,
            keep_generation=None,
            include_generation=False,
        ):
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                close_entered.set()
                await release_close.wait()

        interface._close_desktop_proxy_tunnels_for_workspace = blocked_close
        stale_stop = asyncio.create_task(
            interface._announce_desktop_change(
                workspace_id,
                old,
                {"stopped": True, "generation": 4, "epoch": interface._desktop_epoch},
            )
        )
        await asyncio.wait_for(close_entered.wait(), 1)
        current["session"] = new
        release_close.set()
        await stale_stop
        await interface._announce_desktop_change(
            workspace_id,
            new,
            {"stopped": False, "generation": 5, "epoch": interface._desktop_epoch},
        )
        emitted = [call.args for call in interface._sio.emit.await_args_list]
        self.assertEqual([event for event, _ in emitted], ["desktop:process"])
        self.assertEqual([payload["generation"] for _, payload in emitted], [5])
        self.assertEqual(emitted[0][1]["epoch"], interface._desktop_epoch)

    async def test_default_tunnel_close_removes_unknown_generation_tunnel(self) -> None:
        service = DummyService()
        workspace_id = uuid.uuid4()
        other_workspace_id = uuid.uuid4()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        finalize = AsyncMock()
        interface._finalize_desktop_proxy_tunnel = finalize
        interface._desktop_proxy_tunnels["unknown"] = SimpleNamespace(
            workspace_id=workspace_id, desktop_generation=None
        )
        interface._desktop_proxy_tunnels["known"] = SimpleNamespace(
            workspace_id=workspace_id, desktop_generation=5
        )
        interface._desktop_proxy_tunnels["other-workspace"] = SimpleNamespace(
            workspace_id=other_workspace_id, desktop_generation=None
        )

        await interface._close_desktop_proxy_tunnels_for_workspace(workspace_id)

        self.assertCountEqual(
            [call.args[0] for call in finalize.await_args_list], ["unknown", "known"]
        )

    async def test_concurrent_release_prevents_started_event_after_tunnel_close(
        self,
    ) -> None:
        """A viewer released during tunnel invalidation cannot emit stale start."""
        workspace_id = uuid.uuid4()
        lease = self._viewer_lease(workspace_id)
        old = SimpleNamespace(generation=4)
        started = SimpleNamespace(
            generation=5,
            port=6901,
            viewer_held=True,
            computeruse_run_ids=set(),
        )
        current = {"session": old}
        service = DummyService()
        service.desktop.get_desktop_session = lambda _: current["session"]
        service.desktop.get_desktop_generation = lambda _: 5

        async def desktop_action(_workspace_id, action, _args=None):
            if action == "hold":
                current["session"] = started
                return {
                    "ok": True,
                    "port": 6901,
                    "generation": 5,
                    "epoch": "runner-desktop-epoch",
                }
            current["session"] = None
            return {
                "ok": True,
                "stopped": True,
                "process_alive": False,
                "generation": 5,
                "epoch": "runner-desktop-epoch",
            }

        service.desktop.desktop_action = AsyncMock(side_effect=desktop_action)
        service.desktop.get_desktop_state_payload = lambda ws_id: (
            {
                "workspace_id": str(ws_id),
                "port": 6901,
                "guest_ip": "10.0.0.5",
                "viewer": True,
                "generation": 5,
                "epoch": "runner-desktop-epoch",
            }
            if current["session"] is not None
            else None
        )
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()
        close_entered = asyncio.Event()
        release_start_close = asyncio.Event()
        close_calls = 0

        async def blocked_close(_workspace_id, **_kwargs):
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                close_entered.set()
                await release_start_close.wait()

        interface._close_desktop_proxy_tunnels_for_workspace = blocked_close
        start = asyncio.create_task(
            interface._sio.handlers["/"]["task:start_desktop"](
                {"task_id": "start-race", "workspace_id": str(workspace_id), **lease}
            )
        )
        await asyncio.wait_for(close_entered.wait(), 1)
        await interface._sio.handlers["/"]["task:stop_desktop"](
            {"task_id": "stop-race", "workspace_id": str(workspace_id), **lease}
        )
        release_start_close.set()
        await start

        emitted = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:stopped", emitted)
        self.assertNotIn("desktop:started", emitted)
        self.assertEqual(
            [call.args[0:2] for call in service.desktop.desktop_action.await_args_list],
            [(workspace_id, "hold"), (workspace_id, "release")],
        )

    async def test_stop_desktop_emits_generation_and_epoch(self) -> None:
        service = DummyService()
        workspace_id = uuid.uuid4()
        lease = self._viewer_lease(workspace_id)
        service.desktop.desktop_action = AsyncMock(
            return_value={
                "ok": True,
                "stopped": True,
                "process_alive": False,
                "viewer_held": False,
                "computer_use_active": False,
                "generation": 7,
                "epoch": "runner-desktop-epoch",
            }
        )
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()

        await interface._sio.handlers["/"]["task:stop_desktop"](
            {"task_id": "task-stop", "workspace_id": str(workspace_id), **lease}
        )

        service.desktop.desktop_action.assert_awaited_once_with(
            workspace_id, "release", lease
        )
        stopped = next(
            call.args[1]
            for call in interface._sio.emit.await_args_list
            if call.args[0] == "desktop:stopped"
        )
        self.assertEqual(stopped["generation"], 7)
        self.assertEqual(stopped["epoch"], "runner-desktop-epoch")

    async def test_stop_desktop_emits_viewer_released_when_computer_use_holds(
        self,
    ) -> None:
        service = DummyService()
        workspace_id = uuid.uuid4()
        from unittest.mock import MagicMock

        session = MagicMock()
        session.generation = 2
        session.viewer_held = True
        session.computeruse_run_ids = {"run-1"}
        service.desktop.sessions[workspace_id] = session
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()
        task_id = "desktop-stop-2"
        lease = self._viewer_lease(workspace_id)

        handler = interface._sio.handlers["/"]["task:stop_desktop"]
        await handler(
            {"task_id": task_id, "workspace_id": str(workspace_id), **lease}
        )

        service.desktop.desktop_action.assert_awaited_once_with(
            workspace_id, "release", lease
        )
        interface._sio.emit.assert_awaited_with(
            "desktop:viewer_released",
            {
                "ok": True,
                "stopped": False,
                "process_alive": True,
                "viewer_held": False,
                "computer_use_active": True,
                "generation": 2,
                "epoch": "runner-desktop-epoch",
                "task_id": task_id,
                "workspace_id": str(workspace_id),
            },
        )

    async def test_desktop_proxy_http_request_uses_runner_local_fetch(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._fetch_desktop_http = AsyncMock(
            return_value={
                "status": 200,
                "headers": [["Content-Type", "text/plain"]],
                "body": "ZGVza3RvcA==",
                "body_encoding": "base64",
            }
        )

        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["desktop:proxy_http_request"]
        result = await handler(
            {
                "workspace_id": str(workspace_id),
                "path": "/vnc.html",
                "query_string": "autoconnect=true",
            }
        )

        interface._fetch_desktop_http.assert_awaited_once_with(
            workspace_id,
            "/vnc.html",
            "autoconnect=true",
        )
        self.assertEqual(result["status"], 200)

    async def test_desktop_proxy_ws_open_uses_runner_local_tunnel(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._open_desktop_proxy_tunnel = AsyncMock(
            return_value={"ok": True, "subprotocol": "binary"}
        )

        workspace_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["desktop:proxy_ws_open"]
        result = await handler(
            {
                "workspace_id": str(workspace_id),
                "tunnel_id": "tunnel-1",
                "subprotocols": ["binary"],
            }
        )

        interface._open_desktop_proxy_tunnel.assert_awaited_once_with(
            workspace_id,
            "tunnel-1",
            ["binary"],
        )
        self.assertEqual(result, {"ok": True, "subprotocol": "binary"})

    async def test_desktop_proxy_ws_send_forwards_payload(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._send_desktop_proxy_tunnel_message = AsyncMock()

        handler = interface._sio.handlers["/"]["desktop:proxy_ws_send"]
        await handler(
            {
                "tunnel_id": "tunnel-1",
                "data": "aGVsbG8=",
                "encoding": "base64",
            }
        )

        interface._send_desktop_proxy_tunnel_message.assert_awaited_once_with(
            "tunnel-1",
            text=None,
            data="aGVsbG8=",
            encoding="base64",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "deleted",
        "already_absent",
        RuntimeError("blocked"),
    ],
)
async def test_image_deletion_uses_runtime_reference(
    tmp_path: Path, outcome: str | Exception
) -> None:
    """Image deletion has no workspace dependency and retains durable results."""
    service = DummyService()
    delete = service.images.delete_image_reference
    delete.side_effect = outcome if isinstance(outcome, Exception) else None
    delete.return_value = outcome
    interface = WebSocketInterface(service, RunnerSettings(state_dir=str(tmp_path)))
    interface._operations.emit = AsyncMock()
    try:
        await interface._sio.handlers["/"]["task:delete_image_artifact"](
            {
                "task_id": "delete",
                "operation_id": "delete",
                "attempt": 1,
                "runner_id": "runner",
                "target": "image",
                "image_instance_id": "image",
                "image_artifact_id": "physical-reference",
            }
        )
        delete.assert_awaited_once_with(image_ref="physical-reference")
        event, result = interface._operations.journal.pending()[0]
        assert result["execution_finished"]
        if isinstance(outcome, Exception):
            assert event == "image_artifact:delete_failed"
            assert result["outcome_known"]
        else:
            assert event == "image_artifact:deleted"
            assert result["image_instance_id"] == "image"
            assert result["already_absent"] == (outcome == "already_absent")
    finally:
        interface._operations.journal.close()


class WebSocketCloneWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_clone_failure_emits_workspace_id(self) -> None:
        service = DummyService()
        service.images.create_workspace_from_image_artifact = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._operations.emit = AsyncMock()

        task_id = "clone-task-1"
        workspace_id = uuid.uuid4()
        payload = {
            "task_id": task_id,
            "workspace_id": str(workspace_id),
            "image_artifact_id": str(uuid.uuid4()),
            "agent_type": "copilot",
            "env_vars": {},
            "ssh_keys": [],
        }

        handler = interface._sio.handlers["/"][
            "task:create_workspace_from_image_artifact"
        ]
        payload.update(
            operation_id=task_id,
            attempt=1,
            target=str(workspace_id),
            runner_id="runner",
        )
        await handler(payload)

        packet = interface._operations.emit.await_args.args
        self.assertEqual(packet[0], "operation:result")
        self.assertEqual(packet[1]["event"], "workspace:error")
        self.assertEqual(packet[1]["data"]["workspace_id"], str(workspace_id))
        self.assertIn("execution finished", packet[1]["data"]["error"])
        self.assertTrue(packet[1]["data"]["outcome_known"])


class WebSocketCreateWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_env_and_ssh_credentials(self) -> None:
        service = DummyService()

        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._operations.emit = AsyncMock()

        task_id = "create-task-1"
        workspace_id = uuid.uuid4()
        payload = {
            "task_id": task_id,
            "workspace_id": str(workspace_id),
            "repos": ["git@github.com:example/private-repo.git"],
            "base_image_path": "/images/base.qcow2",
            "env_vars": {"GITHUB_TOKEN": "secret"},
            "ssh_keys": [
                "-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"
            ],
            "configure_commands": [],
        }

        handler = interface._sio.handlers["/"]["task:create_workspace"]
        payload.update(
            operation_id=task_id,
            attempt=1,
            target=str(workspace_id),
            runner_id="runner",
        )
        await handler(payload)

        self.assertEqual(len(service.create_workspace_calls), 1)
        forwarded = service.create_workspace_calls[0]
        self.assertEqual(forwarded["env_vars"], payload["env_vars"])
        self.assertEqual(forwarded["ssh_keys"], payload["ssh_keys"])


class WebSocketCredentialInjectTests(unittest.IsolatedAsyncioTestCase):
    async def test_inject_credentials_acks_presence_and_emits_event(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()
        task_id = "inject-task-1"
        workspace_id = uuid.uuid4()

        handler = interface._sio.handlers["/"]["task:inject_credentials"]
        result = await handler(
            {
                "task_id": task_id,
                "workspace_id": str(workspace_id),
                "env_vars": {"GITHUB_TOKEN": "secret"},
                "files": [],
                "ssh_keys": [],
            }
        )

        self.assertEqual(result, {"ok": True, "credentials_present": True})
        interface._sio.emit.assert_awaited_with(
            "workspace:credentials_injected",
            {
                "task_id": task_id,
                "workspace_id": str(workspace_id),
                "credentials_present": True,
            },
        )

    async def test_inject_credentials_empty_set_acks_absent(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()
        workspace_id = uuid.uuid4()

        handler = interface._sio.handlers["/"]["task:inject_credentials"]
        result = await handler(
            {
                "task_id": "inject-task-empty",
                "workspace_id": str(workspace_id),
                "env_vars": {},
                "files": [],
                "ssh_keys": [],
            }
        )

        self.assertEqual(result, {"ok": True, "credentials_present": False})


if __name__ == "__main__":
    unittest.main()
