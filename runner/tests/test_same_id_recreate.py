"""Same-id remove+create and capture-of-capture independence."""

import json
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from docker.errors import NotFound

from src.journal import Journal
from src.runtime.base import WorkspaceConfig
from src.runtime.docker_runtime import DockerRuntime


def qemu_modules():
    """Load QemuRuntime, stubbing libvirt when the optional extra is absent."""
    import sys
    import types

    try:
        import libvirt
    except ImportError:
        libvirt = types.ModuleType("libvirt")
        libvirt.VIR_DOMAIN_SHUTOFF = 5
        libvirt.VIR_DOMAIN_RUNNING = 1
        libvirt.VIR_DOMAIN_UNDEFINE_NVRAM = 4
        libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE = 2
        libvirt.VIR_ERR_NO_DOMAIN = 42

        class LibvirtError(Exception):
            def get_error_code(self):
                return None

        libvirt.libvirtError = LibvirtError
        sys.modules["libvirt"] = libvirt
    from src.runtime.qemu_runtime import QemuRuntime, libvirt as runtime_libvirt

    return QemuRuntime, runtime_libvirt


def runtime(tmp_path):
    QemuRuntime, _libvirt = qemu_modules()
    r = object.__new__(QemuRuntime)
    r._snapshot_dir = tmp_path / "snapshots"
    r._disk_dir = tmp_path / "disks"
    images = tmp_path / "images"
    for directory in (r._snapshot_dir, r._disk_dir, images):
        directory.mkdir()
    r._settings = SimpleNamespace(qemu_image_cache_dir=str(images))
    r._libvirt_conn = MagicMock()
    r._libvirt_conn.return_value.listAllDomains.return_value = []
    return r


def qcow(path, backing=None):
    command = ["qemu-img", "create", "-f", "qcow2"]
    if backing:
        command += ["-F", "qcow2", "-b", str(backing)]
    subprocess.run(command + [str(path), "8M"], check=True, capture_output=True)


def qemu_ready(tmp_path):
    r = runtime(tmp_path)
    r._ssh_connections = {}
    r._ssh_locks = {}
    r._host_key_cache = {}
    r._destroy_workspace_network = AsyncMock()
    r._create_workspace_network = AsyncMock(
        return_value=("10.100.1.1", "10.100.1.2")
    )
    r._wait_for_ssh = AsyncMock(return_value="10.100.1.2")
    r._settings = SimpleNamespace(
        qemu_image_cache_dir=str(tmp_path / "images"),
        qemu_vcpus=2,
        qemu_memory_mb=2048,
        qemu_disk_size_gb=1,
    )

    async def write_iso(instance_id, **_kwargs):
        path = r._cloud_init_iso_path(instance_id)
        path.write_bytes(b"cidata")
        return path

    r._create_cloud_init_iso = write_iso
    return r


@pytest.mark.asyncio
async def test_same_id_remove_then_create_clears_disk_iso_key(tmp_path):
    r = qemu_ready(tmp_path)
    workspace_id = str(uuid.uuid4())
    base = r._snapshot_dir / "base.qcow2"
    qcow(base)
    disk = r._disk_path(workspace_id)
    qcow(disk, base)
    iso = r._cloud_init_iso_path(workspace_id)
    iso.write_bytes(b"cidata")
    key = r._host_key_file(workspace_id)
    key.write_text("10.100.1.2 ssh-ed25519 AAAA")
    r._host_key_cache[workspace_id] = key.read_text()
    r._get_domain = lambda _: (_ for _ in ()).throw(RuntimeError("absent"))

    await r.remove_workspace(workspace_id)
    assert not disk.exists()
    assert not iso.exists()
    assert not key.exists()
    assert workspace_id not in r._host_key_cache
    r._destroy_workspace_network.assert_awaited_once_with(workspace_id)

    domain = MagicMock()
    r._libvirt_conn.return_value.defineXML.return_value = domain
    created = await r.create_workspace(
        WorkspaceConfig(
            workspace_id=workspace_id,
            image=str(base),
            env_vars={},
            qemu_disk_size_gb=1,
        )
    )
    assert created == workspace_id
    assert r._disk_path(workspace_id).exists()
    assert r._cloud_init_iso_path(workspace_id).exists()
    r._create_workspace_network.assert_awaited_with(workspace_id)
    domain.create.assert_called_once()
    info = await r._image_info(r._disk_path(workspace_id))
    assert info.get("backing-filename")


@pytest.mark.asyncio
async def test_same_id_artifact_clone_after_remove(tmp_path):
    r = qemu_ready(tmp_path)
    workspace_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())
    capture = r._snapshot_dir / f"{artifact_id}.qcow2"
    qcow(capture)
    capture.with_suffix(".meta").write_text(f"snapshot_id={artifact_id}\n")
    leftover = r._disk_path(workspace_id)
    qcow(leftover, capture)
    r._get_domain = lambda _: (_ for _ in ()).throw(RuntimeError("absent"))

    await r.remove_workspace(workspace_id)
    assert not leftover.exists()

    domain = MagicMock()
    r._libvirt_conn.return_value.defineXML.return_value = domain
    created = await r.create_workspace_from_image_artifact(
        artifact_id, workspace_id, qemu_disk_size_gb=1
    )
    assert created == workspace_id
    disk = r._disk_path(workspace_id)
    assert disk.exists()
    info = await r._image_info(disk)
    assert info.get("backing-filename")
    r._create_workspace_network.assert_awaited_with(workspace_id)


@pytest.mark.asyncio
async def test_capture_of_capture_has_no_backing_file(tmp_path):
    _QemuRuntime, libvirt = qemu_modules()
    r = qemu_ready(tmp_path)
    domain = MagicMock()
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    r._get_domain = lambda _: domain

    first_disk = r._disk_dir / "source.qcow2"
    qcow(first_disk)
    first_id = str(uuid.uuid4())
    first = await r.create_image_artifact(
        "source",
        "v1",
        artifact_id=first_id,
        operation_id="op-1",
        credential_clean=True,
    )
    first_path = r._snapshot_dir / f"{first_id}.qcow2"
    assert first.artifact_id == first_id
    assert not (await r._image_info(first_path)).get("backing-filename")

    child_disk = r._disk_dir / "child.qcow2"
    qcow(child_disk, first_path)
    child_info = await r._image_info(child_disk)
    assert child_info.get("backing-filename")

    second_id = str(uuid.uuid4())
    second = await r.create_image_artifact(
        "child",
        "v2",
        artifact_id=second_id,
        operation_id="op-2",
        credential_clean=True,
    )
    second_path = r._snapshot_dir / f"{second_id}.qcow2"
    published = await r._image_info(second_path)
    assert not published.get("backing-filename")
    assert not published.get("full-backing-filename")
    assert first_path.exists()
    assert json.loads(second_path.with_suffix(".manifest.json").read_text())[
        "self_contained"
    ]
    assert second.artifact_id == second_id


@pytest.mark.asyncio
async def test_docker_same_id_remove_then_create_rebuilds_volume_and_network():
    runtime = DockerRuntime()
    client = runtime._client = MagicMock()
    workspace_id = str(uuid.uuid4())
    volume_name = f"opencuria-workspace-{workspace_id}"
    network_name = f"opencuria-ws-{workspace_id}"
    labels = runtime._volume_labels(workspace_id)
    present = {"container": None, "volume": False, "network": False}

    volume = MagicMock()
    volume.name = volume_name
    volume.attrs = {"Labels": labels}

    def drop_volume():
        present["volume"] = False

    volume.remove.side_effect = drop_volume

    network = MagicMock()
    network.attrs = {
        "Labels": {"opencuria.workspace-id": workspace_id},
        "Containers": {},
    }

    def drop_network():
        present["network"] = False

    network.remove.side_effect = drop_network

    def volumes_get(_name):
        if present["volume"]:
            return volume
        raise NotFound("absent")

    def networks_get(_name):
        if present["network"]:
            return network
        raise NotFound("absent")

    def volumes_create(**_kwargs):
        present["volume"] = True
        return volume

    def networks_create(**_kwargs):
        present["network"] = True
        return network

    def containers_run(**kwargs):
        container = MagicMock()
        container.id = f"cid-{uuid.uuid4().hex[:8]}"
        container.name = kwargs["name"]
        container.labels = kwargs["labels"]
        container.attrs = {"Mounts": []}

        def drop_container(**_remove):
            present["container"] = None

        container.remove.side_effect = drop_container
        present["container"] = container
        return container

    def containers_get(instance_id):
        current = present["container"]
        if current is None:
            raise NotFound("absent")
        if instance_id in {current.id, current.name, workspace_id}:
            return current
        raise NotFound("absent")

    def volumes_list(**_kwargs):
        return [volume] if present["volume"] else []

    def containers_list(**_kwargs):
        current = present["container"]
        return [current] if current is not None else []

    client.volumes.get.side_effect = volumes_get
    client.volumes.create.side_effect = volumes_create
    client.volumes.list.side_effect = volumes_list
    client.networks.get.side_effect = networks_get
    client.networks.create.side_effect = networks_create
    client.containers.run.side_effect = containers_run
    client.containers.get.side_effect = containers_get
    client.containers.list.side_effect = containers_list

    config = WorkspaceConfig(
        workspace_id,
        "alpine:3.21",
        {},
        {volume_name: {"bind": "/workspace", "mode": "rw"}},
    )
    first = await runtime.create_workspace(config)
    first_container = present["container"]
    assert first == first_container.id
    client.volumes.create.assert_called_once_with(name=volume_name, labels=labels)
    client.networks.create.assert_called_once()
    assert client.containers.run.call_args.kwargs["name"] == volume_name
    assert client.containers.run.call_args.kwargs["network"] == network_name

    await runtime.remove_workspace(first)
    first_container.remove.assert_called_once_with(force=True)
    volume.remove.assert_called_once()
    network.remove.assert_called_once()
    assert present == {"container": None, "volume": False, "network": False}

    client.volumes.create.reset_mock()
    client.networks.create.reset_mock()
    client.containers.run.reset_mock()
    second = await runtime.create_workspace(config)
    assert second != first
    client.volumes.create.assert_called_once_with(name=volume_name, labels=labels)
    assert client.networks.create.call_args.kwargs["name"] == network_name
    assert client.containers.run.call_args.kwargs["name"] == volume_name
    assert client.containers.run.call_args.kwargs["network"] == network_name


def test_journal_checkpoint_can_be_replaced_after_same_id_remove(tmp_path):
    journal = Journal(str(tmp_path))
    workspace_id = str(uuid.uuid4())
    previous = {"disks": [{"path": "/old.qcow2"}]}
    replacement = {"disks": [{"path": "/new.qcow2"}]}
    journal.checkpoint(
        workspace_id,
        previous,
        {
            "state": "exited",
            "event": "scrubbed",
            "credentials_present": False,
        },
    )
    assert journal.proof(workspace_id, previous)
    journal.invalidate_checkpoint(workspace_id)
    assert journal.proof(workspace_id, previous) is None
    journal.checkpoint(
        workspace_id,
        replacement,
        {
            "state": "running",
            "event": "workspace:created",
            "credentials_present": True,
            "initialized": True,
        },
    )
    assert journal.proof(workspace_id, replacement)
    journal.close()
