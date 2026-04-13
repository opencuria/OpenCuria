import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.runtime.base import CommandExecutionError


async def _empty_output():
    if False:
        yield ""


class DummyService:
    def __init__(self) -> None:
        self.supported_runtimes = []
        self.run_configure_commands = AsyncMock()
        self.terminate_prompt_process = AsyncMock()
        self.cleanup_prompt_process_tracking = AsyncMock()
        self.sync_from_runtime = AsyncMock()
        self.run_health_check_loop = AsyncMock()
        self.get_workspace_heartbeat_statuses = AsyncMock(return_value=[])
        self.prepared_operation = object()
        self.run_command_calls = []
        self.run_command_side_effects: list[object] = []
        self.create_workspace_calls = []
        self.start_desktop = AsyncMock()
        self.get_desktop_container_ip = lambda workspace_id: "127.0.0.1"
        self.get_desktop_network_name = lambda workspace_id: "workspace-net"

    def _normalise_command_args(self, command_args):
        return command_args

    async def run_command(self, workspace_id, command, prepared=None):
        self.run_command_calls.append((workspace_id, command, prepared))
        effect = self.run_command_side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        async for line in _empty_output():
            yield line

    async def prepare_operation(self, workspace_id, env_vars=None, ssh_keys=None):
        return self.prepared_operation

    async def cleanup_operation(self, prepared):
        return None

    async def create_workspace_from_image_artifact(self, **kwargs):
        return kwargs["new_workspace_id"]

    async def create_workspace(self, **kwargs):
        self.create_workspace_calls.append(kwargs)
        return kwargs.get("workspace_id") or uuid.uuid4()


class WebSocketRunPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_missing_command_exit_code(self) -> None:
        service = DummyService()
        service.run_command_side_effects = [
            CommandExecutionError(127),
            None,
        ]

        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()

        workspace_id = uuid.uuid4()
        task_id = "task-123"
        payload = {
            "task_id": task_id,
            "workspace_id": str(workspace_id),
            "command": {
                "args": ["claude", "--print", "hello"],
                "workdir": "/workspace",
                "env": {},
                "description": "Run prompt with Claude Code",
            },
            "fallback_configure_commands": [
                {
                    "args": [
                        "bash",
                        "-lc",
                        "npm install -g @anthropic-ai/claude-code",
                    ],
                    "description": "Install Claude Code CLI",
                }
            ],
        }

        handler = interface._sio.handlers["/"]["task:run_prompt"]
        await handler(payload)
        await interface._running_tasks[task_id]

        service.run_configure_commands.assert_awaited_once_with(
            workspace_id,
            payload["fallback_configure_commands"],
            prepared=service.prepared_operation,
        )
        self.assertEqual(len(service.run_command_calls), 2)
        service.cleanup_prompt_process_tracking.assert_awaited_once()

    async def test_does_not_retry_for_non_missing_command_exit_code(self) -> None:
        service = DummyService()
        service.run_command_side_effects = [CommandExecutionError(1)]

        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()

        workspace_id = uuid.uuid4()
        task_id = "task-456"
        payload = {
            "task_id": task_id,
            "workspace_id": str(workspace_id),
            "command": {
                "args": ["claude", "--print", "hello"],
                "workdir": "/workspace",
                "env": {},
                "description": "Run prompt with Claude Code",
            },
            "fallback_configure_commands": [
                {
                    "args": [
                        "bash",
                        "-lc",
                        "npm install -g @anthropic-ai/claude-code",
                    ],
                    "description": "Install Claude Code CLI",
                }
            ],
        }

        handler = interface._sio.handlers["/"]["task:run_prompt"]
        await handler(payload)
        await interface._running_tasks[task_id]

        service.run_configure_commands.assert_not_awaited()
        self.assertEqual(len(service.run_command_calls), 1)
        service.cleanup_prompt_process_tracking.assert_awaited_once()
        self.assertIs(service.run_command_calls[0][2], service.prepared_operation)

    async def test_cancelling_task_maps_sigterm_exit_to_user_cancelled(self) -> None:
        service = DummyService()
        release_command = asyncio.Event()

        async def _run_command(workspace_id, command, prepared=None):  # noqa: ANN001
            service.run_command_calls.append((workspace_id, command, prepared))
            yield "start"
            await release_command.wait()
            raise CommandExecutionError(143)
            yield ""  # pragma: no cover

        service.run_command = _run_command

        interface = WebSocketInterface(service, RunnerSettings())
        interface._sio.emit = AsyncMock()

        workspace_id = uuid.uuid4()
        run_task_id = "run-task-1"
        run_payload = {
            "task_id": run_task_id,
            "workspace_id": str(workspace_id),
            "command": {
                "args": ["bash", "-lc", "echo start; sleep 120; echo end"],
                "workdir": "/workspace",
                "env": {},
                "description": "Deterministic long-running test command",
            },
        }

        run_handler = interface._sio.handlers["/"]["task:run_prompt"]
        await run_handler(run_payload)
        interface._cancelling_task_ids.add(run_task_id)
        release_command.set()
        await interface._running_tasks[run_task_id]

        emitted = [call.args for call in interface._sio.emit.await_args_list]
        self.assertIn(
            (
                "output:error",
                {
                    "task_id": run_task_id,
                    "workspace_id": str(workspace_id),
                    "error": "Prompt execution cancelled by user",
                },
            ),
            emitted,
        )


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
    async def test_start_desktop_emits_qemu_proxy_metadata(self) -> None:
        service = DummyService()
        service.start_desktop = AsyncMock(return_value=type("Session", (), {"port": 6901})())
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
            },
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
