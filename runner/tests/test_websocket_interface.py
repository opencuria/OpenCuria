import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface


class DummyService:
    def __init__(self) -> None:
        self.supported_runtimes = []
        self.sync_from_runtime = AsyncMock()
        self.recover_desktop_sessions_from_runtime = AsyncMock()
        self.run_health_check_loop = AsyncMock()
        self.get_workspace_heartbeat_statuses = AsyncMock(return_value=[])
        self.create_workspace_calls = []
        self.start_desktop = AsyncMock(
            return_value=type(
                "Session",
                (),
                {"port": 6901, "viewer_held": True, "computeruse_run_ids": set()},
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
                },
            )()
        )
        self.get_desktop_container_ip = lambda workspace_id: "127.0.0.1"
        self.get_desktop_network_name = lambda workspace_id: "workspace-net"

    async def create_workspace_from_image_artifact(self, **kwargs):
        return kwargs["new_workspace_id"], bool(
            kwargs.get("env_vars") or kwargs.get("ssh_keys") or kwargs.get("files")
        )

    async def create_workspace(self, **kwargs):
        self.create_workspace_calls.append(kwargs)
        return kwargs.get("workspace_id") or uuid.uuid4(), bool(
            kwargs.get("env_vars") or kwargs.get("ssh_keys") or kwargs.get("files")
        )


class WebSocketLegacyPromptRemovedTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_prompt_handler_is_gone(self) -> None:
        """Negative test: task:run_prompt no longer exists on the runner."""
        interface = WebSocketInterface(DummyService(), RunnerSettings())
        assert "task:run_prompt" not in interface._sio.handlers["/"]

    async def test_cancel_prompt_handler_is_gone(self) -> None:
        """Negative test: task:cancel_prompt no longer exists on the runner."""
        interface = WebSocketInterface(DummyService(), RunnerSettings())
        assert "task:cancel_prompt" not in interface._sio.handlers["/"]


class WebSocketMetricsPathTests(unittest.TestCase):
    def test_storage_root_defaults_to_var_lib_opencuria(self) -> None:
        settings = RunnerSettings(
            qemu_image_cache_dir="/var/lib/opencuria/images",
            qemu_disk_dir="/var/lib/opencuria/disks",
            qemu_snapshot_dir="/var/lib/opencuria/snapshots",
        )
        interface = WebSocketInterface(DummyService(), settings)
        self.assertEqual(interface._storage_root_path().as_posix(), "/var/lib/opencuria")

    def test_storage_root_respects_custom_common_base(self) -> None:
        settings = RunnerSettings(
            qemu_image_cache_dir="/mnt/kern-store/images",
            qemu_disk_dir="/mnt/kern-store/disks",
            qemu_snapshot_dir="/mnt/kern-store/snapshots",
        )
        interface = WebSocketInterface(DummyService(), settings)
        self.assertEqual(interface._storage_root_path().as_posix(), "/mnt/kern-store")

    def test_resolve_disk_usage_path_falls_back_to_existing_parent(self) -> None:
        settings = RunnerSettings(
            qemu_image_cache_dir="/tmp/runner-metrics-test/images",
            qemu_disk_dir="/tmp/runner-metrics-test/disks",
            qemu_snapshot_dir="/tmp/runner-metrics-test/snapshots",
        )
        interface = WebSocketInterface(DummyService(), settings)
        self.assertEqual(interface._resolve_disk_usage_path(), "/tmp")


class WebSocketDesktopTests(unittest.IsolatedAsyncioTestCase):
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

        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()

        await interface._sio.handlers["/"]["connect"]()

        service.sync_from_runtime.assert_awaited_once()
        service.recover_desktop_sessions_from_runtime.assert_awaited_once()
        interface._sio.emit.assert_any_await(
            "desktop:process",
            {
                "workspace_id": service.get_workspace_heartbeat_statuses.return_value[0]["workspace_id"],
                "port": 6901,
                "container_ip": "127.0.0.1",
                "network_name": "workspace-net",
                "viewer": False,
                "computer_use": False,
            },
        )

    async def test_start_desktop_emits_qemu_proxy_metadata(self) -> None:
        service = DummyService()
        service.start_desktop = AsyncMock(
            return_value=type(
                "Session",
                (),
                {"port": 6901, "viewer_held": True, "computeruse_run_ids": set()},
            )()
        )
        service.get_desktop_container_ip = lambda workspace_id: "10.100.0.2"
        service.get_desktop_network_name = lambda workspace_id: ""

        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()

        task_id = "desktop-task-1"
        workspace_id = uuid.uuid4()

        handler = interface._sio.handlers["/"]["task:start_desktop"]
        await handler({"task_id": task_id, "workspace_id": str(workspace_id)})

        service.start_desktop.assert_awaited_once_with(workspace_id)
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
            },
        )

    async def test_stop_desktop_emits_stopped_when_process_ends(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()
        task_id = "desktop-stop-1"
        workspace_id = uuid.uuid4()

        handler = interface._sio.handlers["/"]["task:stop_desktop"]
        await handler({"task_id": task_id, "workspace_id": str(workspace_id)})

        service.stop_desktop.assert_awaited_once_with(workspace_id)
        interface._sio.emit.assert_awaited_with(
            "desktop:stopped",
            {
                "task_id": task_id,
                "workspace_id": str(workspace_id),
            },
        )

    async def test_stop_desktop_emits_viewer_released_when_computer_use_holds(
        self,
    ) -> None:
        service = DummyService()
        service.stop_desktop = AsyncMock(
            return_value=type(
                "Release",
                (),
                {
                    "stopped": False,
                    "process_alive": True,
                    "viewer_held": False,
                    "computer_use_active": True,
                },
            )()
        )
        interface = WebSocketInterface(service, RunnerSettings())
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
            },
        )

    async def test_desktop_proxy_http_request_uses_runner_local_fetch(self) -> None:
        service = DummyService()
        interface = WebSocketInterface(service, RunnerSettings())
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
        interface = WebSocketInterface(service, RunnerSettings())
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
        interface = WebSocketInterface(service, RunnerSettings())
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
        service.create_workspace_from_image_artifact = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()

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
        await handler(payload)

        interface._sio.emit.assert_awaited_with(
            "workspace:error",
            {
                "task_id": task_id,
                "workspace_id": str(workspace_id),
                "error": "boom",
            },
        )


class WebSocketCreateWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_env_and_ssh_credentials(self) -> None:
        service = DummyService()

        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()

        task_id = "create-task-1"
        workspace_id = uuid.uuid4()
        payload = {
            "task_id": task_id,
            "workspace_id": str(workspace_id),
            "repos": ["git@github.com:example/private-repo.git"],
            "runtime_type": "docker",
            "image_tag": "opencuria/workspace:test",
            "env_vars": {"GITHUB_TOKEN": "secret"},
            "ssh_keys": ["-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"],
            "configure_commands": [],
        }

        handler = interface._sio.handlers["/"]["task:create_workspace"]
        await handler(payload)

        self.assertEqual(len(service.create_workspace_calls), 1)
        forwarded = service.create_workspace_calls[0]
        self.assertEqual(forwarded["env_vars"], payload["env_vars"])
        self.assertEqual(forwarded["ssh_keys"], payload["ssh_keys"])


if __name__ == "__main__":
    unittest.main()
