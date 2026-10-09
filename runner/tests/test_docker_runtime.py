import threading
import unittest
from unittest.mock import MagicMock, patch

from docker.errors import NotFound

from src.runtime.base import WorkspaceConfig
from src.runtime.docker_runtime import DockerRuntime


class DockerRuntimeAgentStdinTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_bootstrap_frame_keeps_docker_stdin_open(self) -> None:
        from src.runtime.managed_process import isolated_agent_env_preamble

        runtime = DockerRuntime()
        raw_sock = MagicMock()
        sock = MagicMock()
        sock._sock = raw_sock
        api = MagicMock()
        api.exec_create.return_value = {"Id": "exec-agent"}
        api.exec_start.return_value = sock
        runtime._client = MagicMock(api=api)
        env = {
            "ANTHROPIC_API_KEY": "test-secret",
            "CLAUDE_CONFIG_DIR": "/workspace/.opencuria/harness/claude/12345678-1234-1234-1234-123456789abc/config",
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": "0.2.164",
            "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
            "DISABLE_UPDATES": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        }
        pump_started = threading.Event()

        def start_pump(*args, **kwargs):
            pump_started.set()
            # Keep the pump thread inert; socket closure is not part of spawn.

        with patch.object(
            runtime, "_drain_docker_stream_socket", side_effect=start_pump
        ):
            handle = await runtime.spawn_process(
                "instance",
                ["/usr/bin/claude", "--input-format", "stream-json"],
                workdir="/workspace",
                env=env,
                control_path="/var/lib/opencuria/streams/test",
                expected_token={"boot_id": "boot", "init_starttime": "1"},
                isolated_env=True,
                isolated_home="/tmp/opencuria-agent-home-0123456789abcdef0123456789abcdef",
            )

        assert pump_started.wait(1)
        raw_sock.sendall.assert_called_once_with(isolated_agent_env_preamble(env))
        raw_sock.shutdown.assert_not_called()
        assert handle.handle is sock
        stop = handle.metadata["stop"]
        stop.set()
        handle.metadata["pump"].join(timeout=1)


class DockerRuntimeNetworkIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_workspace_creates_isolated_network_and_attaches_container(
        self,
    ) -> None:
        runtime = DockerRuntime()
        client = MagicMock()
        runtime._client = client

        client.networks.get.side_effect = NotFound("missing")
        client.volumes.get.side_effect = NotFound("missing")
        container = MagicMock()
        container.id = "container-1234567890"
        client.containers.run.return_value = container

        config = WorkspaceConfig(
            workspace_id="ws-1",
            image="opencuria/workspace:latest",
            env_vars={},
            volumes={"opencuria-workspace-ws-1": {"bind": "/workspace", "mode": "rw"}},
            labels={"opencuria.workspace-id": "ws-1"},
        )

        instance_id = await runtime.create_workspace(config)

        self.assertEqual(instance_id, "container-1234567890")
        client.networks.create.assert_called_once_with(
            name="opencuria-ws-ws-1",
            driver="bridge",
            check_duplicate=True,
            internal=False,
            labels={
                "opencuria.runtime-type": "docker",
                "opencuria.isolated-network": "true",
                "opencuria.workspace-id": "ws-1",
            },
        )
        client.containers.run.assert_called_once()
        self.assertEqual(
            client.containers.run.call_args.kwargs["network"], "opencuria-ws-ws-1"
        )
        self.assertEqual(
            client.containers.run.call_args.kwargs["labels"][
                "opencuria.workspace-network"
            ],
            "opencuria-ws-ws-1",
        )

    async def test_remove_workspace_removes_container_and_isolated_network(
        self,
    ) -> None:
        runtime = DockerRuntime()
        client = MagicMock()
        runtime._client = client

        container = MagicMock()
        container.labels = {"opencuria.workspace-id": "ws-1"}
        client.containers.get.return_value = container

        network = MagicMock()
        network.attrs = {"Labels": {"opencuria.workspace-id": "ws-1"}, "Containers": {}}
        client.networks.get.side_effect = [network, network, NotFound("removed")]

        await runtime.remove_workspace("container-1234567890")

        container.remove.assert_called_once_with(force=True)
        self.assertEqual(client.networks.get.call_count, 3)
        network.remove.assert_called_once_with()

    async def test_create_workspace_cleans_up_network_if_container_start_fails(
        self,
    ) -> None:
        runtime = DockerRuntime()
        client = MagicMock()
        runtime._client = client

        client.networks.get.side_effect = NotFound("missing")
        client.containers.run.side_effect = RuntimeError("boom")

        config = WorkspaceConfig(
            workspace_id="ws-1",
            image="opencuria/workspace:latest",
            env_vars={},
            labels={"opencuria.workspace-id": "ws-1"},
        )

        created_network = MagicMock()
        created_network.attrs = {"Labels": {"opencuria.workspace-id": "ws-1"}}
        client.networks.create.return_value = created_network
        client.networks.get.side_effect = [
            NotFound("missing"),
            created_network,
            NotFound("removed"),
        ]

        with self.assertRaises(RuntimeError):
            await runtime.create_workspace(config)

        created_network.remove.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
