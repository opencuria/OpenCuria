import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import SOCKETIO_MAX_HTTP_BUFFER_SIZE, WebSocketInterface


class DummyService:
    def __init__(self) -> None:
        self.supported_runtimes = []
        self.sync_from_runtime = AsyncMock()
        self.recover_desktop_sessions_from_runtime = AsyncMock()
        self.run_health_check_loop = AsyncMock()
        self.get_workspace_heartbeat_statuses = AsyncMock(return_value=[])
        self.create_workspace_calls = []
        # Step 2: websocket terminal + image handlers call the managers
        # directly (``service.terminal_manager`` / ``service.images``).
        # The dummy mirrors that surface so handler tests exercise the
        # same path; the underlying mocks stay directly awaitable.
        self.terminal_manager = AsyncMock()
        self.images = AsyncMock()
        self.images.create_workspace_from_image_artifact = AsyncMock(
            side_effect=self._create_workspace_from_image_artifact
        )
        self.sync_from_runtime = AsyncMock()
        self.recover_desktop_sessions_from_runtime = AsyncMock()
        self.run_health_check_loop = AsyncMock()
        self.get_workspace_heartbeat_statuses = AsyncMock(return_value=[])
        self.create_workspace_calls = []
        # Step 6b: websocket desktop handlers call the manager directly
        # (``service.desktop``). The dummy mirrors that surface so handler
        # tests exercise the same path; the underlying mocks stay directly
        # awaitable. Facade shims delegate to the same impls.
        self.desktop = self._FakeDesktop(self)
        self.start_desktop = self.desktop.start_desktop
        self.stop_desktop = self.desktop.stop_desktop
        self.get_desktop_container_ip = self.desktop.get_desktop_container_ip
        self.get_desktop_network_name = self.desktop.get_desktop_network_name

    class _FakeDesktop:
        def __init__(self, outer) -> None:
            self._outer = outer
            self.start_desktop = AsyncMock(
                return_value=type(
                    "Session",
                    (),
                    {
                        "port": 6901,
                        "viewer_held": True,
                        "computeruse_run_ids": set(),
                        "generation": 0,
                        "epoch": "legacy-desktop-epoch",
                    },
                )()
            )
            self.stop_desktop = AsyncMock(
                return_value=type(
                    "Release",
                    (),
                    {
                        "stopped": True,
                        "process_alive": False,
                        "viewer_held": False,
                        "computer_use_active": False,
                        "generation": 0,
                        "epoch": "legacy-desktop-epoch",
                    },
                )()
            )

        def get_desktop_generation(self, workspace_id):
            return 0

        def get_desktop_container_ip(self, workspace_id):
            return "127.0.0.1"

        def get_desktop_network_name(self, workspace_id):
            return "workspace-net"

        def get_desktop_session(self, workspace_id):
            """Stub session lookup: no cached session (cold interface path)."""
            return

    def get_desktop_generation(self, workspace_id):
        return 0

    def get_desktop_session(self, workspace_id):
        """Stub session lookup: no cached session (cold interface path)."""
        return

    async def _create_workspace_from_image_artifact(self, **kwargs):
        return kwargs["new_workspace_id"], bool(
            kwargs.get("env_vars") or kwargs.get("ssh_keys") or kwargs.get("files")
        )

    async def create_workspace_from_image_artifact(self, **kwargs):
        return await self._create_workspace_from_image_artifact(**kwargs)

    async def create_workspace(self, **kwargs):
        self.create_workspace_calls.append(kwargs)
        return kwargs.get("workspace_id") or uuid.uuid4(), bool(
            kwargs.get("env_vars") or kwargs.get("ssh_keys") or kwargs.get("files")
        )

    async def inject_credentials(self, workspace_id, **kwargs):
        return bool(
            kwargs.get("env_vars") or kwargs.get("ssh_keys") or kwargs.get("files")
        )


class WebSocketLegacyPromptRemovedTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_prompt_handler_is_gone(self) -> None:
        """Negative test: task:run_prompt no longer exists on the runner."""
        interface = WebSocketInterface(
            DummyService(), RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        assert "task:run_prompt" not in interface._sio.handlers["/"]

    async def test_cancel_prompt_handler_is_gone(self) -> None:
        """Negative test: task:cancel_prompt no longer exists on the runner."""
        interface = WebSocketInterface(
            DummyService(), RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        assert "task:cancel_prompt" not in interface._sio.handlers["/"]


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


class WebSocketDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_snapshot_is_reused_for_first_heartbeat(self) -> None:
        """Connect snapshot is not immediately re-probed for the first heartbeat."""
        import asyncio
        import contextlib

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
                    "runtime_type": "docker",
                    "desktop": {
                        "port": 6901,
                        "container_ip": "127.0.0.1",
                        "network_name": "workspace-net",
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
                "supported_runtimes": [],
                "status": "ready",
                "inventory_epoch": interface._inventory_epoch,
                "desktop_leases": {
                    "protocol_version": 1,
                    "epoch": interface._inventory_epoch,
                },
            },
        )
        await interface.stop()

    async def test_blocked_inventory_does_not_block_health_or_operation_lease(self):
        import asyncio

        service = DummyService()
        blocked = asyncio.Event()
        entered = asyncio.Event()

        async def scan():
            entered.set()
            await blocked.wait()

        service.inventory_for_runner = AsyncMock(side_effect=scan)
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
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
            # Disk/host initialization on a shared CI runner is not a one-second
            # performance contract. Prove independence while the scan remains
            # blocked, rather than racing that unrelated initialization.
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

    async def test_start_desktop_emits_qemu_proxy_metadata(self) -> None:
        service = DummyService()
        service.desktop.start_desktop = AsyncMock(
            return_value=type(
                "Session",
                (),
                {
                    "port": 6901,
                    "viewer_held": True,
                    "computeruse_run_ids": set(),
                    "generation": 0,
                    "epoch": "legacy-desktop-epoch",
                },
            )()
        )
        service.start_desktop = service.desktop.start_desktop
        service.desktop.get_desktop_session = (
            lambda _workspace_id: service.desktop.start_desktop.return_value
        )
        service.desktop.get_desktop_container_ip = lambda workspace_id: "10.100.0.2"
        service.get_desktop_container_ip = service.desktop.get_desktop_container_ip
        service.desktop.get_desktop_network_name = lambda workspace_id: ""
        service.get_desktop_network_name = service.desktop.get_desktop_network_name

        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        service.desktop.get_desktop_session = (
            lambda _: service.desktop.start_desktop.return_value
        )
        interface._sio.emit = AsyncMock()

        task_id = "desktop-task-1"
        workspace_id = uuid.uuid4()

        handler = interface._sio.handlers["/"]["task:start_desktop"]
        await handler({"task_id": task_id, "workspace_id": str(workspace_id)})

        service.desktop.start_desktop.assert_awaited_once_with(
            workspace_id, width=None, height=None
        )
        interface._sio.emit.assert_awaited_with(
            "desktop:started",
            {
                "task_id": task_id,
                "workspace_id": str(workspace_id),
                "port": 6901,
                "container_ip": "10.100.0.2",
                "network_name": "",
                "viewer": True,
                "computer_use": False,
                "generation": 0,
                "epoch": interface._desktop_epoch,
            },
        )

    async def test_stop_desktop_emits_stopped_when_process_ends(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()
        task_id = "desktop-stop-1"
        workspace_id = uuid.uuid4()

        handler = interface._sio.handlers["/"]["task:stop_desktop"]
        await handler({"task_id": task_id, "workspace_id": str(workspace_id)})

        service.desktop.stop_desktop.assert_awaited_once_with(workspace_id)
        interface._sio.emit.assert_awaited_with(
            "desktop:stopped",
            {
                "task_id": task_id,
                "workspace_id": str(workspace_id),
                "generation": 0,
                "epoch": "legacy-desktop-epoch",
            },
        )

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
            "container_ip": "10.0.0.5",
            "network_name": "net",
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

    async def test_concurrent_stop_prevents_started_event_after_tunnel_close(
        self,
    ) -> None:
        import asyncio

        service = DummyService()
        workspace_id = uuid.uuid4()
        old = SimpleNamespace(generation=4)
        started = SimpleNamespace(
            generation=5,
            port=6901,
            viewer_held=True,
            computeruse_run_ids=set(),
        )
        current = {"session": old}
        service.desktop.get_desktop_session = lambda _: current["session"]
        service.desktop.get_desktop_generation = lambda _: 5

        async def start_desktop(*_args, **_kwargs):
            current["session"] = started
            return started

        service.desktop.start_desktop = AsyncMock(side_effect=start_desktop)
        service.desktop.stop_desktop = AsyncMock(
            return_value=SimpleNamespace(
                stopped=True,
                process_alive=False,
                generation=5,
                epoch="legacy-desktop-epoch",
            )
        )
        service.desktop.get_desktop_container_ip = lambda _: "10.0.0.5"
        service.desktop.get_desktop_network_name = lambda _: "net"
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
            else:
                current["session"] = None

        interface._close_desktop_proxy_tunnels_for_workspace = blocked_close
        start = asyncio.create_task(
            interface._sio.handlers["/"]["task:start_desktop"](
                {"task_id": "start-race", "workspace_id": str(workspace_id)}
            )
        )
        await asyncio.wait_for(close_entered.wait(), 1)
        await interface._sio.handlers["/"]["task:stop_desktop"](
            {"task_id": "stop-race", "workspace_id": str(workspace_id)}
        )
        release_start_close.set()
        await start

        emitted = [call.args[0] for call in interface._sio.emit.await_args_list]
        self.assertIn("desktop:stopped", emitted)
        self.assertNotIn("desktop:started", emitted)

    async def test_stop_desktop_emits_generation_and_epoch(self) -> None:
        service = DummyService()
        workspace_id = uuid.uuid4()
        service.desktop.get_desktop_session = lambda _: None
        service.desktop.get_desktop_state_payload = lambda _: None
        service.desktop.stop_desktop = AsyncMock(
            return_value=SimpleNamespace(
                stopped=True,
                process_alive=False,
                viewer_held=False,
                computer_use_active=False,
                generation=7,
                epoch="runner-desktop-epoch",
            )
        )
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()

        await interface._sio.handlers["/"]["task:stop_desktop"](
            {"task_id": "task-stop", "workspace_id": str(workspace_id)}
        )

        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "desktop:stopped")
        self.assertEqual(payload["generation"], 7)
        self.assertEqual(payload["epoch"], "runner-desktop-epoch")

    async def test_stop_desktop_emits_viewer_released_when_computer_use_holds(
        self,
    ) -> None:
        service = DummyService()
        service.desktop.stop_desktop = AsyncMock(
            return_value=type(
                "Release",
                (),
                {
                    "stopped": False,
                    "process_alive": True,
                    "viewer_held": False,
                    "computer_use_active": True,
                    "generation": 0,
                    "epoch": "legacy-desktop-epoch",
                },
            )()
        )
        service.stop_desktop = service.desktop.stop_desktop
        interface = WebSocketInterface(
            service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        interface._sio.emit = AsyncMock()
        task_id = "desktop-stop-2"
        workspace_id = uuid.uuid4()

        handler = interface._sio.handlers["/"]["task:stop_desktop"]
        await handler({"task_id": task_id, "workspace_id": str(workspace_id)})

        interface._sio.emit.assert_awaited_with(
            "desktop:viewer_released",
            {
                "task_id": task_id,
                "workspace_id": str(workspace_id),
                "computer_use_active": True,
                "generation": 0,
                "epoch": "legacy-desktop-epoch",
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
            "runtime_type": "qemu",
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
            "runtime_type": "docker",
            "image_tag": "opencuria/workspace:test",
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
