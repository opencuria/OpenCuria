"""Deletion never confuses workspace ownership with a convenient volume name."""

import uuid
from unittest.mock import MagicMock

import pytest
from docker.errors import NotFound

from src.runtime.base import WorkspaceConfig
from src.runtime.docker_runtime import DockerRuntime


@pytest.fixture
def fixture():
    rt = DockerRuntime()
    client = rt._client = MagicMock()
    wid = str(uuid.uuid4())
    container = client.containers.get.return_value
    container.id = "exact-container"
    container.name = f"opencuria-workspace-{wid}"
    container.labels = {"opencuria.workspace-id": wid}
    container.attrs = {"Mounts": []}
    volume = MagicMock()
    volume.name = container.name
    volume.attrs = {"Labels": rt._volume_labels(wid)}
    client.volumes.list.return_value = [volume]
    client.containers.list.return_value = [container]
    client.networks.get.side_effect = NotFound("absent")
    return rt, client, wid, container, volume


@pytest.mark.asyncio
async def test_volume_failure_after_container_removed_retries_by_uuid(fixture):
    rt, client, wid, container, volume = fixture
    volume.remove.side_effect = RuntimeError("daemon failure")
    with pytest.raises(RuntimeError, match="daemon failure"):
        await rt.remove_workspace(container.id)
    container.remove.assert_called_once_with(force=True)
    client.containers.get.side_effect = NotFound("absent")
    client.containers.list.return_value = []
    volume.remove.side_effect = None
    client.volumes.get.side_effect = NotFound("absent")
    await rt.remove_workspace(wid)
    assert volume.remove.call_count == 2


@pytest.mark.asyncio
async def test_shared_volume_blocks_before_destroy_even_force(fixture):
    rt, client, _, container, volume = fixture
    foreign = MagicMock(id="other")
    client.containers.list.return_value = [container, foreign]
    with pytest.raises(RuntimeError, match="external consumers"):
        await rt.remove_workspace(container.id)
    container.remove.assert_not_called()
    volume.remove.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_volume_never_unlinked(fixture):
    rt, _, _, container, volume = fixture
    volume.attrs["Labels"]["owner"] = "foreign"
    with pytest.raises(RuntimeError, match="Foreign workspace volume"):
        await rt.remove_workspace(container.id)
    container.remove.assert_not_called()
    volume.remove.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_requires_exact_mount_and_identity(fixture):
    rt, client, _, container, volume = fixture
    client.volumes.list.return_value = []
    volume.attrs = {"Labels": None}
    container.attrs["Mounts"] = [
        {"Type": "volume", "Name": volume.name, "Destination": "/workspace"}
    ]
    client.volumes.get.side_effect = [volume, NotFound("removed")]
    await rt.remove_workspace(container.id)
    volume.remove.assert_called_once_with()


@pytest.mark.asyncio
async def test_bind_and_shared_mounts_are_not_deleted(fixture):
    rt, client, _, container, volume = fixture
    client.volumes.list.return_value = []
    container.attrs["Mounts"] = [
        {"Type": "bind", "Name": volume.name, "Destination": "/workspace"},
        {"Type": "volume", "Name": "user-shared", "Destination": "/other"},
    ]
    await rt.remove_workspace(container.id)
    client.volumes.get.assert_not_called()
    volume.remove.assert_not_called()


@pytest.mark.asyncio
async def test_creation_labels_only_canonical_config(fixture):
    rt, client, wid, _, _ = fixture
    client.volumes.get.side_effect = NotFound("absent")
    cfg = WorkspaceConfig(
        wid,
        "alpine:3.21",
        {},
        {f"opencuria-workspace-{wid}": {"bind": "/workspace", "mode": "rw"}},
    )
    await rt.create_workspace(cfg)
    client.volumes.create.assert_called_once_with(
        name=f"opencuria-workspace-{wid}", labels=rt._volume_labels(wid)
    )


@pytest.mark.asyncio
async def test_absent_legacy_volume_refuses_false_success(fixture):
    rt, client, wid, _, volume = fixture
    client.containers.get.side_effect = NotFound("absent")
    client.volumes.list.return_value = []
    client.volumes.get.return_value = volume
    with pytest.raises(RuntimeError, match="Unattributed legacy"):
        await rt.remove_workspace(wid)
    volume.remove.assert_not_called()


@pytest.mark.asyncio
async def test_network_failure_is_not_success(fixture):
    rt, client, wid, container, _ = fixture
    client.volumes.list.return_value = []
    network = MagicMock()
    network.attrs = {"Labels": {"opencuria.workspace-id": wid}, "Containers": {}}
    network.remove.side_effect = RuntimeError("network busy")
    client.networks.get.return_value = network
    client.networks.get.side_effect = None
    with pytest.raises(RuntimeError, match="network busy"):
        await rt.remove_workspace(container.id)


@pytest.mark.asyncio
async def test_foreign_network_blocks_before_container_remove(fixture):
    rt, client, _, container, _ = fixture
    network = MagicMock()
    network.attrs = {"Labels": {"opencuria.workspace-id": "foreign"}}
    client.networks.get.side_effect = None
    client.networks.get.return_value = network
    with pytest.raises(RuntimeError, match="foreign workspace network"):
        await rt.remove_workspace(container.id)
    container.remove.assert_not_called()


@pytest.mark.asyncio
async def test_canonical_mount_must_not_be_redirected(fixture):
    rt, client, wid, _, _ = fixture
    cfg = WorkspaceConfig(
        wid,
        "alpine:3.21",
        {},
        {f"opencuria-workspace-{wid}": {"bind": "/other", "mode": "rw"}},
    )
    with pytest.raises(RuntimeError, match="Invalid canonical"):
        await rt.create_workspace(cfg)
    client.containers.run.assert_not_called()
