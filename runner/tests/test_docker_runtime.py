import unittest
from unittest.mock import MagicMock

from docker.errors import NotFound

from src.runtime.docker_runtime import DockerRuntime
from src.runtime.base import WorkspaceConfig


class DockerRuntimeNetworkIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_workspace_creates_isolated_network_and_attaches_container(self) -> None:
        runtime = DockerRuntime()
        client = MagicMock()
        runtime._client = client

        client.networks.get.side_effect = NotFound("missing")
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
        self.assertEqual(client.containers.run.call_args.kwargs["network"], "opencuria-ws-ws-1")
        self.assertEqual(
            client.containers.run.call_args.kwargs["labels"]["opencuria.workspace-network"],
            "opencuria-ws-ws-1",
        )

    async def test_remove_workspace_removes_container_and_isolated_network(self) -> None:
        runtime = DockerRuntime()
        client = MagicMock()
        runtime._client = client

        container = MagicMock()
        container.labels = {"opencuria.workspace-id": "ws-1"}
        client.containers.get.return_value = container

        network = MagicMock()
        client.networks.get.return_value = network

        await runtime.remove_workspace("container-1234567890")

        container.remove.assert_called_once_with(force=True)
        client.networks.get.assert_called_once_with("opencuria-ws-ws-1")
        network.remove.assert_called_once_with()

    async def test_create_workspace_cleans_up_network_if_container_start_fails(self) -> None:
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
        client.networks.create.return_value = created_network
        client.networks.get.side_effect = [NotFound("missing"), created_network]

        with self.assertRaises(RuntimeError):
            await runtime.create_workspace(config)

        created_network.remove.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
