"""Guest lifecycle isolation and durable publication failure regressions."""

import asyncio
import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import libvirt
import pytest
from pydantic import ValidationError

from src.config import RunnerSettings
from src.runtime.base import WorkspaceConfig
from src.runtime.storage import storage_mutation
from tests import test_parallel_image_builds
from tests.test_parallel_image_builds import build
from tests.test_storage_inventory import qcow, runtime

builder = test_parallel_image_builds.builder


async def test_guest_readiness_allows_inventory_and_other_guest_lifecycles(builder):
    r = builder
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_ssh(instance_id):
        if instance_id == first_id:
            entered.set()
            await release.wait()

    r._wait_for_ssh.side_effect = wait_for_ssh
    base = r._resolve_build_base_image.return_value
    first = asyncio.create_task(
        r.create_workspace(WorkspaceConfig(workspace_id=first_id, image=str(base)))
    )
    await asyncio.wait_for(entered.wait(), 5)
    same_guest = asyncio.create_task(r.stop_workspace(first_id))
    try:
        scan = await asyncio.wait_for(r.inventory(), 3)
        assert scan.complete, scan.errors
        await asyncio.wait_for(
            r.create_workspace(
                WorkspaceConfig(workspace_id=second_id, image=str(base))
            ),
            3,
        )
        assert not same_guest.done()
        domain = r._get_domain(second_id)
        domain.suspend()
        await asyncio.wait_for(r.start_workspace(second_id), 3)
        assert domain.state()[0] == libvirt.VIR_DOMAIN_RUNNING
        domain.suspend()
        await asyncio.wait_for(r.stop_workspace(second_id), 3)
        assert domain.state()[0] == libvirt.VIR_DOMAIN_SHUTOFF
    finally:
        release.set()
        await first
        await same_guest
        for instance_id in (first_id, second_id):
            await r.remove_workspace(instance_id)


async def test_duplicate_create_keeps_existing_disk_and_domain(builder):
    r = builder
    workspace_id = str(uuid.uuid4())
    config = WorkspaceConfig(
        workspace_id=workspace_id, image=str(r._resolve_build_base_image.return_value)
    )
    await r.create_workspace(config)
    disk = r._disk_path(workspace_id)
    before = disk.stat()
    domain_uuid = r._get_domain(workspace_id).UUIDString()
    try:
        with pytest.raises(FileExistsError, match="already exists"):
            await r.create_workspace(config)
        after = disk.stat()
        assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
        assert r._get_domain(workspace_id).UUIDString() == domain_uuid
    finally:
        await r.remove_workspace(workspace_id)


@pytest.mark.parametrize("corruption", ["hash", "size", "path", "bytes", "backing"])
async def test_clone_rejects_invalid_publication_before_allocating_guest(
    tmp_path, corruption
):
    r = runtime(tmp_path)
    disk = r._disk_path("source")
    qcow(disk)
    target = r._snapshot_dir / "published.qcow2"
    await r._publish_image(disk, target, {"operation_id": "capture"})
    marker = target.with_suffix(".manifest.json")
    manifest = json.loads(marker.read_text())
    if corruption == "bytes":
        with target.open("r+b") as stream:
            stream.seek(-1, 2)
            stream.write(b"!")
    elif corruption == "backing":
        target.unlink()
        qcow(target, disk)
        manifest.update(
            size_bytes=target.stat().st_size, sha256=r._publication_digest(target)
        )
        marker.write_text(json.dumps(manifest))
    else:
        key, invalid = {
            "hash": ("sha256", "bad"),
            "size": ("size_bytes", 0),
            "path": ("image_path", str(r._snapshot_dir / "foreign.qcow2")),
        }[corruption]
        marker.write_text(json.dumps({**manifest, key: invalid}))
    r._create_workspace_network = AsyncMock()
    with pytest.raises(RuntimeError, match="manifest|self-contained"):
        await r.create_workspace_from_image_artifact("published", "clone")
    assert not r._disk_path("clone").exists()
    r._create_workspace_network.assert_not_awaited()


async def test_small_workspace_requests_reject_before_allocating_disk_or_network(
    builder,
):
    r = builder
    base = r._resolve_build_base_image.return_value
    await asyncio.to_thread(
        subprocess.run,
        ["qemu-img", "resize", str(base), "2G"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(RuntimeError, match="image minimum 2 GiB"):
        await r.create_workspace(
            WorkspaceConfig(
                workspace_id="too-small", image=str(base), qemu_disk_size_gb=1
            )
        )
    assert not r._disk_path("too-small").exists()
    assert not any(
        network.name().endswith("too-small")
        for network in r._libvirt_conn().listAllNetworks(0)
    )
    overlay = await r._create_overlay_disk(
        "overlay", disk_size_gb=2, base_image=str(base)
    )
    assert (await r._image_info(overlay))["virtual-size"] == 2 * 1024**3
    with pytest.raises(FileExistsError):
        await r._create_overlay_disk("overlay", disk_size_gb=2, base_image=str(base))
    target = r._snapshot_dir / "published.qcow2"
    published = await r._publish_image(overlay, target, {"operation_id": "capture"})
    assert published["min_disk_size_gb"] == 2
    assert published["virtual_size_bytes"] == 2 * 1024**3
    try:
        with pytest.raises(RuntimeError, match="image minimum 2 GiB"):
            await r.create_workspace_from_image_artifact(
                "published", "clone", qemu_disk_size_gb=1
            )
        assert not r._disk_path("clone").exists()
        await r.create_workspace_from_image_artifact(
            "published", "clone", qemu_disk_size_gb=2
        )
        disk = r._disk_path("clone")
        assert (await r._image_info(disk))["virtual-size"] == 2 * 1024**3
        assert disk.stat().st_mode & 0o777 == 0o644
    finally:
        await r.remove_workspace("clone")
        overlay.unlink()


async def test_slow_publication_fsync_keeps_event_loop_responsive_and_drains_cancel(
    tmp_path, monkeypatch
):
    r = runtime(tmp_path)
    source = r._disk_path("source")
    qcow(source)
    target = r._snapshot_dir / "target.qcow2"
    entered, release = threading.Event(), threading.Event()
    fsync = os.fsync

    def delayed(fd):
        entered.set()
        assert release.wait(5), "test did not release fsync"
        fsync(fd)

    monkeypatch.setattr(os, "fsync", delayed)
    task = asyncio.create_task(r._publish_image(source, target, {"operation_id": "op"}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # This timeout would fail if fsync ran directly on the event loop.
        await asyncio.wait_for(asyncio.sleep(0), 0.2)
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    manifest = json.loads(target.with_suffix(".manifest.json").read_text())
    assert manifest["sha256"] == r._publication_digest(target)


async def test_slow_inventory_filesystem_probe_does_not_block_heartbeat(
    tmp_path, monkeypatch
):
    r = runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    statvfs = os.statvfs

    def delayed(path):
        entered.set()
        assert release.wait(5), "test did not release filesystem probe"
        return statvfs(path)

    monkeypatch.setattr(os, "statvfs", delayed)
    task = asyncio.create_task(r.inventory())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.wait_for(asyncio.sleep(0), 0.2)
    finally:
        release.set()
    assert (await task).complete


async def test_child_task_does_not_inherit_storage_mutation_permission():
    entered, release, child_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children = []

    class Runtime:
        @storage_mutation
        async def parent(self):
            children.append(asyncio.create_task(self.child()))
            entered.set()
            await release.wait()

        @storage_mutation
        async def child(self):
            child_entered.set()

    r = Runtime()
    task = asyncio.create_task(r.parent())
    await entered.wait()
    try:
        await asyncio.sleep(0.02)
        assert not child_entered.is_set()
    finally:
        release.set()
    await task
    await asyncio.gather(*children)
    assert child_entered.is_set()


async def test_build_budget_queues_guests_and_uses_separate_cpu_ram(builder):
    r = builder
    r._settings.qemu_build_concurrency = 1
    r._settings.qemu_build_vcpus = 1
    r._settings.qemu_build_memory_mb = 1024
    entered, release = asyncio.Event(), asyncio.Event()
    domains = []

    async def provision(instance_id, command, progress):
        domain = r._get_domain(instance_id)
        domains.append(domain)
        assert domain.info()[1] == 1024 * 1024
        assert domain.maxVcpus() == 1
        entered.set()
        await release.wait()

    r._stream_ssh_process.side_effect = provision
    first = build(r)
    await asyncio.wait_for(entered.wait(), 5)
    second = build(r)
    try:
        await asyncio.sleep(0.03)
        assert len(domains) == 1
        assert len(r._libvirt_conn().listAllDomains(0)) == 1
        assert r._resolve_build_base_image.call_count == 1
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert len(domains) == 2
    assert r._libvirt_conn().listAllDomains(0) == []


@pytest.mark.parametrize(
    "field", [
        "qemu_build_vcpus", "qemu_build_memory_mb", "qemu_build_concurrency",
        "qemu_build_queue_timeout",
    ]
)
@pytest.mark.parametrize("value", [0, -1])
def test_build_budget_rejects_non_positive_values(field, value):
    with pytest.raises(ValidationError):
        RunnerSettings(_env_file=None, **{field: value})


@pytest.mark.parametrize("pressure", ["configured", "available"])
async def test_build_memory_pressure_rejects_before_allocating_resources(
    tmp_path, monkeypatch, pressure
):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.runtime import qemu_runtime

    r = runtime(tmp_path)
    r._settings.qemu_memory_mb = 4096
    r._settings.qemu_build_memory_mb = 2048
    guest = MagicMock()
    guest.isActive.return_value = True
    guest.info.return_value = (libvirt.VIR_DOMAIN_RUNNING, 6 * 1024**2, 0, 1, 0)
    r._libvirt_conn.return_value.listAllDomains.return_value = (
        [guest] if pressure == "configured" else []
    )
    monkeypatch.setattr(
        qemu_runtime.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(
            total=8 * 1024**3,
            available=(7 if pressure == "configured" else 2) * 1024**3,
        ),
    )
    r._create_workspace_network = AsyncMock()
    with pytest.raises(RuntimeError, match="Insufficient host memory"):
        await r._prepare_build_instance("new-build", tmp_path / "base.qcow2")
    r._create_workspace_network.assert_not_awaited()
    assert list(r._disk_dir.iterdir()) == []


async def test_build_memory_budget_ignores_stopped_domains(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.runtime import qemu_runtime

    r = runtime(tmp_path)
    r._settings.qemu_memory_mb = 4096
    r._settings.qemu_build_memory_mb = 2048
    guest = MagicMock()
    guest.isActive.return_value = False
    r._libvirt_conn.return_value.listAllDomains.return_value = [guest]
    monkeypatch.setattr(
        qemu_runtime.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=8 * 1024**3, available=7 * 1024**3),
    )
    await asyncio.to_thread(r._assert_build_memory_available)
    guest.info.assert_not_called()


async def test_cloud_init_readonly_probe_retries_running_and_ssh_restart(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from src.runtime import qemu_runtime

    r = runtime(tmp_path)
    ssh = SimpleNamespace(
        run=AsyncMock(
            side_effect=[
                SimpleNamespace(exit_status=0, stdout='{"status":"running"}'),
                OSError("cloud-init restarted SSH"),
                SimpleNamespace(exit_status=2, stdout='{"status":"done","errors":[]}'),
            ]
        )
    )
    r._get_ssh = AsyncMock(return_value=ssh)
    monkeypatch.setattr(qemu_runtime.asyncio, "sleep", AsyncMock())
    await r._wait_for_cloud_init("build")
    assert ssh.run.await_count == 3
    assert all(
        call.args == ("cloud-init status --format=json",)
        for call in ssh.run.await_args_list
    )


@pytest.mark.parametrize(
    "status",
    [
        SimpleNamespace(exit_status=1, stdout='{"status":"error"}'),
        SimpleNamespace(exit_status=0, stdout='{"status":"disabled"}'),
        SimpleNamespace(exit_status=0, stdout='{"status":"done","errors":["failure"]}'),
        SimpleNamespace(exit_status=None, stdout='{"status":"done"}'),
        SimpleNamespace(exit_status=0, stdout="invalid-json"),
    ],
)
async def test_cloud_init_rejects_failed_unknown_and_invalid_completion(
    tmp_path, status
):
    r = runtime(tmp_path)
    ssh = SimpleNamespace(run=AsyncMock(return_value=status))
    r._get_ssh = AsyncMock(return_value=ssh)
    with pytest.raises(RuntimeError, match="cloud-init"):
        await r._wait_for_cloud_init("build")


async def test_cloud_init_wait_is_bounded_and_failed_build_never_runs_script(builder):
    r = builder
    # Exercise the real gate against a guest that never finishes bootstrap.
    r._settings.qemu_cloud_init_timeout = 0.02
    from src.runtime.qemu_runtime import QemuRuntime

    r._wait_for_cloud_init = QemuRuntime._wait_for_cloud_init.__get__(r)
    ssh = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(exit_status=0, stdout='{"status":"running"}')
        )
    )
    r._get_ssh = AsyncMock(return_value=ssh)
    with pytest.raises(RuntimeError, match="did not complete"):
        await asyncio.wait_for(build(r), 3)
    r._stream_ssh_process.assert_not_awaited()
    r.put_archive.assert_not_awaited()
    assert r._libvirt_conn().listAllDomains(0) == []
    assert not r._build_image_dir.exists() or not list(r._build_image_dir.iterdir())


async def test_domain_discovery_failure_is_never_authoritative_absence(tmp_path):
    from unittest.mock import MagicMock

    r = runtime(tmp_path)
    domain = MagicMock()
    domain.name.return_value = "opencuria-workspace-guest"
    domain.state.side_effect = libvirt.libvirtError("connection lost during scan")
    r._libvirt_conn.return_value.listAllDomains.return_value = [domain]
    with pytest.raises(libvirt.libvirtError, match="connection lost"):
        await r.list_workspaces()


@pytest.mark.parametrize(
    "resource", ["disk", "iso", "seed", "host-key", "symlink", "domain", "network"]
)
async def test_workspace_absence_requires_every_canonical_resource_gone(
    tmp_path, resource
):
    from unittest.mock import MagicMock

    r = runtime(tmp_path)
    r._libvirt_conn.return_value.listAllNetworks.return_value = []
    instance_id = "removed-guest"
    assert await r.workspace_removed(instance_id)
    paths = {
        "disk": r._disk_path(instance_id),
        "iso": r._cloud_init_iso_path(instance_id),
        "seed": r._disk_dir / f"{instance_id}-cloud-init",
        "host-key": r._host_key_file(instance_id),
        "symlink": r._disk_path(instance_id),
    }
    if resource in ("domain", "network"):
        item = MagicMock()
        if resource == "domain":
            item.name.return_value = r._domain_name(instance_id)
            r._libvirt_conn.return_value.listAllDomains.return_value = [item]
        else:
            item.name.return_value = r._workspace_network_name(instance_id)
            r._libvirt_conn.return_value.listAllNetworks.return_value = [item]
    elif resource == "seed":
        paths[resource].mkdir()
    elif resource == "symlink":
        paths[resource].symlink_to(tmp_path / "missing")
    else:
        paths[resource].touch()
    assert not await r.workspace_removed(instance_id)


@pytest.mark.parametrize("failure", ["domain", "network", "storage"])
async def test_workspace_absence_never_hides_unavailable_observation(tmp_path, failure):
    r = runtime(tmp_path)
    r._libvirt_conn.return_value.listAllNetworks.return_value = []
    if failure == "domain":
        r._libvirt_conn.return_value.listAllDomains.side_effect = OSError(
            "domain offline"
        )
    elif failure == "network":
        r._libvirt_conn.return_value.listAllNetworks.side_effect = OSError(
            "network offline"
        )
    else:
        r._disk_dir.rmdir()
    with pytest.raises(OSError):
        await r.workspace_removed("removed-guest")


async def test_overlay_publication_never_clobbers_a_racing_creator(tmp_path):
    r = runtime(tmp_path)
    bases = [tmp_path / "images" / f"base-{index}.qcow2" for index in range(2)]
    for base in bases:
        qcow(base)
    inspect = r._image_info
    ready = asyncio.Event()
    inspections = 0

    async def concurrent_info(path):
        nonlocal inspections
        info = await inspect(path)
        inspections += 1
        if inspections == 2:
            ready.set()
        await ready.wait()
        return info

    r._image_info = concurrent_info
    results = await asyncio.gather(
        *(
            r._create_overlay_disk("same", disk_size_gb=1, base_image=str(base))
            for base in bases
        ),
        return_exceptions=True,
    )
    winner = next(
        index
        for index, result in enumerate(results)
        if not isinstance(result, Exception)
    )
    assert isinstance(results[1 - winner], FileExistsError)
    info = await inspect(r._disk_path("same"))
    assert info["full-backing-filename"] == str(bases[winner])
    assert (await r.inventory()).complete


def test_cloud_cache_publication_preserves_concurrently_published_inode(
    tmp_path, monkeypatch
):
    from unittest.mock import MagicMock

    from src.runtime import qemu_runtime

    r = runtime(tmp_path)
    target = tmp_path / "images" / "ubuntu-24.04-server-cloudimg-amd64.img"
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.side_effect = [b"downloaded-image", b""]
    winner = []

    def publish_during_download(*args, **kwargs):
        target.write_bytes(b"another-validated-downloader")
        winner.append(target.stat().st_ino)
        return response

    monkeypatch.setattr(qemu_runtime.urllib.request, "urlopen", publish_during_download)
    monkeypatch.setattr(
        qemu_runtime.subprocess,
        "run",
        MagicMock(return_value=SimpleNamespace(stdout=b'{"format":"qcow2"}')),
    )
    assert r._ensure_ubuntu_cloud_image("24.04") == target
    assert target.stat().st_ino == winner[0]
    assert target.read_bytes() == b"another-validated-downloader"
    assert not list(target.parent.glob("*.tmp"))


async def test_concurrent_starts_atomically_reserve_memory_against_other_guests(
    builder, monkeypatch
):
    from src.runtime import qemu_runtime

    r = builder
    monkeypatch.setattr(
        qemu_runtime.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=4 * 1024**3, available=4 * 1024**3),
    )
    r._settings.qemu_memory_mb = 2048
    base = r._resolve_build_base_image.return_value
    instances = [str(uuid.uuid4()), str(uuid.uuid4())]
    try:
        for instance_id in instances:
            await r.create_workspace(
                WorkspaceConfig(workspace_id=instance_id, image=str(base))
            )
            await r.stop_workspace(instance_id)
        results = await asyncio.gather(
            *(r.start_workspace(instance_id) for instance_id in instances),
            return_exceptions=True,
        )
        assert len([result for result in results if result is None]) == 1
        failures = [result for result in results if isinstance(result, RuntimeError)]
        assert len(failures) == 1
        assert "Insufficient host memory" in str(failures[0])
        assert (
            sum(domain.isActive() for domain in r._libvirt_conn().listAllDomains(0))
            == 1
        )
    finally:
        for instance_id in instances:
            await r.remove_workspace(instance_id)


async def test_paused_resume_does_not_reserve_existing_guest_ram_twice(
    tmp_path, monkeypatch
):
    from unittest.mock import MagicMock

    from src.runtime import qemu_runtime

    r = runtime(tmp_path)
    guest = MagicMock()
    guest.isActive.return_value = True
    guest.name.return_value = r._domain_name("paused")
    guest.info.return_value = (libvirt.VIR_DOMAIN_PAUSED, 2 * 1024**2, 0, 1, 0)
    r._libvirt_conn.return_value.listAllDomains.return_value = [guest]
    monkeypatch.setattr(
        qemu_runtime.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=4 * 1024**3, available=512 * 1024**2),
    )
    # No additional memory is allocated when resuming this 2 GiB guest.
    r._assert_memory_available(2048, "paused")
    with pytest.raises(RuntimeError, match="Insufficient host memory"):
        r._assert_memory_available(1024, "new-guest")


@pytest.mark.parametrize("activation", ["create", "clone"])
async def test_workspace_memory_rejection_precedes_disk_and_network_allocation(
    builder, activation
):
    from unittest.mock import MagicMock

    r = builder
    r._assert_memory_available = MagicMock(side_effect=RuntimeError("memory pressure"))
    r._create_workspace_network = AsyncMock()
    base = r._resolve_build_base_image.return_value
    target = r._snapshot_dir / "published.qcow2"
    await r._publish_image(base, target, {"operation_id": "capture"})
    instance_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="memory pressure"):
        if activation == "create":
            await r.create_workspace(
                WorkspaceConfig(workspace_id=instance_id, image=str(base))
            )
        else:
            await r.create_workspace_from_image_artifact("published", instance_id)
    r._create_workspace_network.assert_not_awaited()
    assert not r._disk_path(instance_id).exists()
    assert r._libvirt_conn().listAllDomains(0) == []


@pytest.mark.parametrize(
    "failure", ["enumeration", "xml", "host", "malformed-host", "unknown-prefix"]
)
async def test_subnet_allocation_fails_closed_on_incomplete_network_observation(
    builder, monkeypatch, failure
):
    from unittest.mock import MagicMock

    from src.runtime import qemu_runtime

    r = builder
    connection = MagicMock()
    network = MagicMock()
    network.name.return_value = "opencuria-ws-existing"
    network.XMLDesc.return_value = (
        "<network><ip address='10.100.0.1' netmask='255.255.255.252'/></network>"
    )
    connection.listAllNetworks.return_value = [network]
    connection.listAllDomains.return_value = []
    r._libvirt_conn = lambda: connection
    if failure == "enumeration":
        connection.listAllNetworks.side_effect = libvirt.libvirtError("unavailable")
    elif failure == "xml":
        network.XMLDesc.side_effect = libvirt.libvirtError("unavailable")
    elif failure == "host":
        monkeypatch.setattr(
            qemu_runtime.subprocess,
            "run",
            MagicMock(side_effect=OSError("unavailable")),
        )
    elif failure == "malformed-host":
        monkeypatch.setattr(
            qemu_runtime.subprocess,
            "run",
            MagicMock(return_value=SimpleNamespace(stdout="malformed")),
        )
    else:
        network.XMLDesc.return_value = "<network><ip address='10.100.0.1'/></network>"
    with pytest.raises((RuntimeError, OSError, libvirt.libvirtError)):
        await r._create_workspace_network("new")
    connection.networkDefineXML.assert_not_called()


async def test_slow_image_delete_fsync_is_drained_without_blocking_event_loop(
    tmp_path, monkeypatch
):
    r = runtime(tmp_path)
    source = r._disk_path("source")
    qcow(source)
    target = r._snapshot_dir / "published.qcow2"
    await r._publish_image(source, target, {"operation_id": "capture"})
    entered, release = threading.Event(), threading.Event()
    fsync = os.fsync

    def delayed(fd):
        entered.set()
        assert release.wait(5)
        fsync(fd)

    monkeypatch.setattr(os, "fsync", delayed)
    task = asyncio.create_task(r.delete_image_artifact("published"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.wait_for(asyncio.sleep(0), 0.2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not target.exists()
    assert not target.with_suffix(".manifest.json").exists()


async def test_capture_and_legacy_inventory_report_actual_virtual_disk_floor(tmp_path):
    from unittest.mock import MagicMock

    r = runtime(tmp_path)
    disk = r._disk_path("source")
    qcow(disk)
    domain = MagicMock()
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    r._get_domain = MagicMock(return_value=domain)
    artifact = await r.create_image_artifact(
        "source",
        "measured",
        artifact_id=str(uuid.uuid4()),
        operation_id="capture",
        credential_clean=True,
    )
    assert artifact.virtual_size_bytes == 8 * 1024**2
    assert artifact.min_disk_size_gb == 1
    target = r._managed_image_path(artifact.artifact_id)
    marker = target.with_suffix(".manifest.json")
    legacy = json.loads(marker.read_text())
    legacy.pop("min_disk_size_gb")
    legacy.pop("virtual_size_bytes")
    marker.write_text(json.dumps(legacy))
    scan = await r.inventory()
    assert scan.complete, scan.errors
    resource = next(item for item in scan.resources if item.resource_id == str(target))
    assert resource.state == "ready"
    assert resource.virtual_bytes == 8 * 1024**2
    assert resource.metadata["virtual_size_bytes"] == 8 * 1024**2
    assert resource.metadata["min_disk_size_gb"] == 1


@pytest.mark.parametrize("allocation", ["create", "clone"])
async def test_existing_noncanonical_domain_blocks_fresh_allocation_without_effects(
    builder, allocation
):
    r = builder
    instance_id = str(uuid.uuid4())
    foreign_disk = r._snapshot_dir / "noncanonical.qcow2"
    qcow(foreign_disk)
    domain = r._libvirt_conn().defineXML(f'''<domain type="test">
      <name>{r._domain_name(instance_id)}</name>
      <memory unit="KiB">131072</memory><vcpu>1</vcpu>
      <os><type>hvm</type></os><devices>
      <disk type="file" device="disk"><source file="{foreign_disk}"/>
      <target dev="vda" bus="virtio"/></disk></devices></domain>''')
    domain.create()
    before = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    before_uuid = domain.UUIDString()
    target = r._snapshot_dir / "published.qcow2"
    await r._publish_image(foreign_disk, target, {"operation_id": "capture"})
    try:
        with pytest.raises(FileExistsError, match="domain already exists"):
            if allocation == "create":
                await r.create_workspace(
                    WorkspaceConfig(
                        workspace_id=instance_id,
                        image=str(target),
                    )
                )
            else:
                await r.create_workspace_from_image_artifact("published", instance_id)
        assert domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE) == before
        assert domain.UUIDString() == before_uuid
        assert domain.isActive()
        assert not r._disk_path(instance_id).exists()
        assert not any(
            network.name() == r._workspace_network_name(instance_id)
            for network in r._libvirt_conn().listAllNetworks(0)
        )
    finally:
        domain.destroy()
        domain.undefine()


def _queue_build(r, *, operation_id=None, progress_callback=None):
    """Issue one independently identifiable physical build."""
    image_id = str(uuid.uuid4())
    return asyncio.create_task(
        r.build_image(
            base_distro="ubuntu:24.04",
            init_script="#!/bin/bash\ntrue\n",
            image_path=str(r._build_image_dir / f"{image_id}.qcow2"),
            operation_id=operation_id or str(uuid.uuid4()),
            image_instance_id=image_id,
            progress_callback=progress_callback,
        )
    )


def _memory_pressure(monkeypatch, release):
    """Keep physical host memory deterministic without consuming host RAM."""
    from src.runtime import qemu_runtime

    monkeypatch.setattr(qemu_runtime, "_BUILD_CAPACITY_POLL_INTERVAL", 0.005)
    monkeypatch.setattr(
        qemu_runtime.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(
            total=8 * 1024**3,
            available=(7 if release.is_set() else 1) * 1024**3,
        ),
    )


async def test_build_waits_for_memory_then_runs_without_allocating_while_queued(
    builder, monkeypatch
):
    r = builder
    release, queued = asyncio.Event(), asyncio.Event()
    _memory_pressure(monkeypatch, release)
    progress = []

    async def report(message):
        progress.append(message)
        if "Waiting for host memory" in message:
            queued.set()

    task = _queue_build(r, progress_callback=report)
    try:
        await asyncio.wait_for(queued.wait(), 2)
        assert not r._libvirt_conn().listAllDomains(0)
        assert list(r._disk_dir.iterdir()) == []
        r._resolve_build_base_image.assert_not_called()
        assert (await asyncio.wait_for(r.inventory(), 2)).complete
    finally:
        release.set()
    result = await asyncio.wait_for(task, 5)
    assert Path(result["image_path"]).exists()
    assert progress.count("Waiting for host memory before image build") == 1
    assert not r._libvirt_conn().listAllDomains(0)


async def test_build_queue_cancel_removes_waiter_without_guest(builder, monkeypatch):
    r = builder
    release, queued = asyncio.Event(), asyncio.Event()
    _memory_pressure(monkeypatch, release)

    async def report(message):
        if "Waiting for host memory" in message:
            queued.set()

    task = _queue_build(r, progress_callback=report)
    await asyncio.wait_for(queued.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    release.set()
    await asyncio.sleep(0.02)
    assert not r._libvirt_conn().listAllDomains(0)
    assert list(r._disk_dir.iterdir()) == []
    r._resolve_build_base_image.assert_not_called()
    assert r._build_semaphore._value == 2


async def test_build_slot_wait_cancel_never_starts_second_guest(builder):
    r = builder
    r._settings.qemu_build_concurrency = 1
    entered, release = asyncio.Event(), asyncio.Event()

    async def provision(*args):
        entered.set()
        await release.wait()

    r._stream_ssh_process.side_effect = provision
    first = build(r)
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second = build(r)
        await asyncio.sleep(0.02)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second, 2)
        assert len(r._libvirt_conn().listAllDomains(0)) == 1
    finally:
        release.set()
        await first
        if second is not None and not second.done():
            await second
    assert r._stream_ssh_process.await_count == 1
    assert r._build_semaphore._value == 1


async def test_build_queue_timeout_is_known_failure_without_allocation(
    builder, monkeypatch
):
    r = builder
    r._settings.qemu_build_queue_timeout = 0.025
    _memory_pressure(monkeypatch, asyncio.Event())
    with pytest.raises(RuntimeError, match="queue timed out waiting for host memory"):
        await asyncio.wait_for(build(r), 2)
    assert list(r._disk_dir.iterdir()) == []
    assert not r._libvirt_conn().listAllDomains(0)
    r._resolve_build_base_image.assert_not_called()
    assert r._build_semaphore._value == 2


async def test_build_impossible_memory_fails_immediately(builder, monkeypatch):
    r = builder
    r._settings.qemu_build_memory_mb = 8192
    _memory_pressure(monkeypatch, asyncio.Event())
    with pytest.raises(RuntimeError, match="exceeds the host capacity"):
        await asyncio.wait_for(build(r), 2)
    r._resolve_build_base_image.assert_not_called()
    assert list(r._disk_dir.iterdir()) == []


async def test_build_cpu_budget_limits_parallelism(builder, monkeypatch):
    from src.runtime import qemu_runtime

    r = builder
    monkeypatch.setattr(qemu_runtime.psutil, "cpu_count", lambda: 1)
    r._settings.qemu_build_concurrency = 4
    r._settings.qemu_build_vcpus = 1
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def provision(instance_id, *args):
        observed.append(instance_id)
        entered.set()
        await release.wait()

    r._stream_ssh_process.side_effect = provision
    first, second = build(r), build(r)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(0.02)
        assert len(observed) == 1
        assert len(r._libvirt_conn().listAllDomains(0)) == 1
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert len(observed) == 2


async def test_build_impossible_cpu_fails_immediately(builder, monkeypatch):
    from src.runtime import qemu_runtime

    r = builder
    monkeypatch.setattr(qemu_runtime.psutil, "cpu_count", lambda: 1)
    r._settings.qemu_build_vcpus = 2
    with pytest.raises(RuntimeError, match="logical CPU capacity"):
        await asyncio.wait_for(build(r), 2)
    r._resolve_build_base_image.assert_not_called()
    assert list(r._disk_dir.iterdir()) == []


async def test_build_rechecks_memory_after_capacity_race(builder, monkeypatch):
    from src.runtime import qemu_runtime
    from src.runtime.qemu_runtime import GuestCapacityUnavailable

    r = builder
    monkeypatch.setattr(qemu_runtime, "_BUILD_CAPACITY_POLL_INTERVAL", 0.005)
    prepare = r._prepare_build_instance
    attempts = 0

    async def racing_prepare(instance_id, base_image):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            assert not r._libvirt_conn().listAllDomains(0)
            raise GuestCapacityUnavailable("memory claimed by another activation")
        return await prepare(instance_id, base_image)

    r._prepare_build_instance = AsyncMock(side_effect=racing_prepare)
    result = await asyncio.wait_for(build(r), 5)
    assert Path(result["image_path"]).exists()
    assert attempts == 2
    assert r._stream_ssh_process.await_count == 1
    assert not r._libvirt_conn().listAllDomains(0)


async def test_build_does_not_retry_unknown_libvirt_failure(builder):
    r = builder
    r._prepare_build_instance = AsyncMock(
        side_effect=libvirt.libvirtError("connection lost after allocation")
    )
    r._cleanup_build_instance = AsyncMock()
    with pytest.raises(libvirt.libvirtError, match="connection lost"):
        await asyncio.wait_for(build(r), 3)
    assert r._prepare_build_instance.await_count == 1
    assert r._cleanup_build_instance.await_count == 1
    r._stream_ssh_process.assert_not_awaited()


async def test_build_guest_identity_matches_operation(builder):
    r = builder
    operation_id = str(uuid.uuid4()).upper()
    await asyncio.wait_for(_queue_build(r, operation_id=operation_id), 5)
    instance_id = r._stream_ssh_process.await_args.args[0]
    assert instance_id == f"image-build-{operation_id.lower()}"
    assert not r._libvirt_conn().listAllDomains(0)


async def test_build_invalid_operation_identity_has_no_effects(builder):
    r = builder
    for operation_id in (None, "", "not-a-uuid", "../../guest"):
        with pytest.raises(ValueError, match="valid operation UUID"):
            await r.build_image(
                base_distro="ubuntu:24.04",
                init_script="true",
                image_path=str(r._build_image_dir / f"{uuid.uuid4()}.qcow2"),
                operation_id=operation_id,
                image_instance_id=str(uuid.uuid4()),
            )
    r._resolve_build_base_image.assert_not_called()
    assert not r._libvirt_conn().listAllDomains(0)
    assert list(r._disk_dir.iterdir()) == []


async def test_build_queue_progress_failure_does_not_allocate(builder, monkeypatch):
    r = builder
    _memory_pressure(monkeypatch, asyncio.Event())
    callback = AsyncMock(side_effect=ConnectionError("progress delivery unavailable"))
    with pytest.raises(ConnectionError, match="delivery unavailable"):
        await asyncio.wait_for(_queue_build(r, progress_callback=callback), 2)
    r._resolve_build_base_image.assert_not_called()
    assert list(r._disk_dir.iterdir()) == []
    assert not r._libvirt_conn().listAllDomains(0)


async def test_build_cancel_during_effect_drains_and_releases_slot(builder):
    r = builder
    r._settings.qemu_build_concurrency = 1
    entered, release = asyncio.Event(), asyncio.Event()

    async def provision(*args):
        entered.set()
        await release.wait()

    r._stream_ssh_process.side_effect = provision
    first = build(r)
    await asyncio.wait_for(entered.wait(), 3)
    first.cancel()
    first.cancel()
    second = build(r)
    try:
        await asyncio.sleep(0.02)
        assert not first.done()
        assert len(r._libvirt_conn().listAllDomains(0)) == 1
        assert r._stream_ssh_process.await_count == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, 5)
    assert r._build_semaphore._value == 1
    assert not r._libvirt_conn().listAllDomains(0)


@pytest.mark.parametrize("failure", ["memory", "cpu", "disk", "disk-inspection"])
async def test_invalid_reconfigure_preserves_running_guest(
    builder, monkeypatch, failure
):
    from src.runtime import qemu_runtime

    r = builder
    instance_id = str(uuid.uuid4())
    base = r._resolve_build_base_image.return_value
    await r.create_workspace(WorkspaceConfig(workspace_id=instance_id, image=str(base)))
    domain = r._get_domain(instance_id)
    old_xml = domain.XMLDesc(0)
    old_size = (await r._image_info(r._disk_path(instance_id)))["virtual-size"]
    resources = {"qemu_vcpus": 1, "qemu_memory_mb": 128, "qemu_disk_size_gb": 1}
    if failure == "memory":
        resources["qemu_memory_mb"] = 16384
        monkeypatch.setattr(
            qemu_runtime.psutil, "virtual_memory",
            lambda: SimpleNamespace(total=8 * 1024**3, available=7 * 1024**3),
        )
    elif failure == "cpu":
        resources["qemu_vcpus"] = 0
    elif failure == "disk":
        await asyncio.to_thread(
            subprocess.run,
            ["qemu-img", "resize", str(r._disk_path(instance_id)), "2G"],
            check=True, capture_output=True,
        )
        old_size = 2 * 1024**3
    else:
        r._image_info = AsyncMock(side_effect=OSError("SSD unavailable"))
    r.stop_workspace = AsyncMock(wraps=r.stop_workspace)
    try:
        with pytest.raises((RuntimeError, ValueError, OSError)):
            await r.reconfigure_workspace(instance_id, restart=True, **resources)
        r.stop_workspace.assert_not_awaited()
        assert domain.state()[0] == libvirt.VIR_DOMAIN_RUNNING
        assert domain.XMLDesc(0) == old_xml
        if failure != "disk-inspection":
            assert (await r._image_info(r._disk_path(instance_id)))["virtual-size"] == old_size
        assert not getattr(r, "_memory_reservations", {})
    finally:
        if failure == "disk-inspection":
            del r._image_info
        await r.remove_workspace(instance_id)


async def test_reconfigure_reserves_ram_during_stop_start_gap(builder, monkeypatch):
    from src.runtime import qemu_runtime
    from src.runtime.qemu_runtime import GuestCapacityUnavailable

    r = builder
    instance_id = str(uuid.uuid4())
    base = r._resolve_build_base_image.return_value
    monkeypatch.setattr(
        qemu_runtime.psutil, "virtual_memory",
        lambda: SimpleNamespace(total=4 * 1024**3, available=3 * 1024**3),
    )
    await r.create_workspace(WorkspaceConfig(workspace_id=instance_id, image=str(base)))
    stopped, release = asyncio.Event(), asyncio.Event()
    original = r._reconfigure_workspace

    async def paused_reconfigure(*args, **kwargs):
        stopped.set()
        await release.wait()
        return await original(*args, **kwargs)

    r._reconfigure_workspace = AsyncMock(side_effect=paused_reconfigure)
    task = asyncio.create_task(r.reconfigure_workspace(
        instance_id, qemu_vcpus=1, qemu_memory_mb=2048,
        qemu_disk_size_gb=1, restart=True,
    ))
    try:
        await asyncio.wait_for(stopped.wait(), 3)
        assert r._get_domain(instance_id).state()[0] == libvirt.VIR_DOMAIN_SHUTOFF
        assert r._memory_reservations == {instance_id: 2048}
        with pytest.raises(GuestCapacityUnavailable):
            await asyncio.to_thread(r._assert_memory_available, 2048, "other")
        await asyncio.to_thread(r._assert_memory_available, 2048, instance_id)
    finally:
        release.set()
        await task
        await r.remove_workspace(instance_id)
    assert not r._memory_reservations
    await asyncio.to_thread(r._assert_memory_available, 2048, "other")


async def test_reconfigure_failure_releases_transient_reservation(builder):
    r = builder
    instance_id = str(uuid.uuid4())
    base = r._resolve_build_base_image.return_value
    await r.create_workspace(WorkspaceConfig(workspace_id=instance_id, image=str(base)))
    r._reconfigure_workspace = AsyncMock(side_effect=OSError("disk write failed"))
    try:
        with pytest.raises(OSError, match="disk write failed"):
            await r.reconfigure_workspace(
                instance_id, qemu_vcpus=1, qemu_memory_mb=128,
                qemu_disk_size_gb=1, restart=True,
            )
        assert not r._memory_reservations
        assert r._get_domain(instance_id).state()[0] == libvirt.VIR_DOMAIN_SHUTOFF
    finally:
        await r.remove_workspace(instance_id)


_HYPERVISOR_STATES = [
    ("nostate", libvirt.VIR_DOMAIN_NOSTATE),
    ("running", libvirt.VIR_DOMAIN_RUNNING),
    ("blocked", libvirt.VIR_DOMAIN_BLOCKED),
    ("paused", libvirt.VIR_DOMAIN_PAUSED),
    ("shutdown", libvirt.VIR_DOMAIN_SHUTDOWN),
    ("shutoff", libvirt.VIR_DOMAIN_SHUTOFF),
    ("crashed", libvirt.VIR_DOMAIN_CRASHED),
    ("suspended", libvirt.VIR_DOMAIN_PMSUSPENDED),
]


def _state_runtime(tmp_path, state):
    """Exercise production state decisions, not a second state machine."""
    from unittest.mock import MagicMock

    r = runtime(tmp_path)
    current = [state]
    domain = MagicMock()
    domain.state.side_effect = lambda: (current[0], 0)
    domain.info.return_value = (state, 128 * 1024, 128 * 1024, 1, 0)
    r._get_domain = lambda _: domain
    r._assert_memory_available = MagicMock()
    r._ensure_memory_reclaim = MagicMock()
    r._invalidate_ssh = AsyncMock()
    r._wait_for_ssh = AsyncMock()
    for action in (domain.create, domain.resume, domain.pMWakeup):
        action.side_effect = lambda *args: current.__setitem__(0, libvirt.VIR_DOMAIN_RUNNING)
    for action in (domain.destroy, domain.shutdown):
        action.side_effect = lambda *args: current.__setitem__(0, libvirt.VIR_DOMAIN_SHUTOFF)
    return r, domain, current


@pytest.mark.parametrize("name,state", _HYPERVISOR_STATES, ids=[s[0] for s in _HYPERVISOR_STATES])
async def test_hypervisor_state_start(tmp_path, name, state):
    r, domain, current = _state_runtime(tmp_path, state)
    if name in ("nostate", "shutdown", "crashed"):
        with pytest.raises(RuntimeError, match="cannot start from libvirt state"):
            await r.start_workspace("guest")
        assert current[0] == state
        r._assert_memory_available.assert_not_called()
    else:
        await r.start_workspace("guest")
        assert current[0] == (
            state if name == "blocked" else libvirt.VIR_DOMAIN_RUNNING
        )
    assert domain.create.call_count == int(name == "shutoff")
    assert domain.resume.call_count == int(name == "paused")
    assert domain.pMWakeup.call_count == int(name == "suspended")
    assert r._wait_for_ssh.await_count == int(name in ("shutoff", "paused", "suspended"))
    domain.destroy.assert_not_called()


@pytest.mark.parametrize("name,state", _HYPERVISOR_STATES, ids=[s[0] for s in _HYPERVISOR_STATES])
async def test_hypervisor_state_stop(tmp_path, monkeypatch, name, state):
    from src.runtime import qemu_runtime

    r, domain, current = _state_runtime(tmp_path, state)

    async def shutdown_progress(*args):
        current[0] = libvirt.VIR_DOMAIN_SHUTOFF

    monkeypatch.setattr(qemu_runtime.asyncio, "sleep", shutdown_progress)
    await r.stop_workspace("guest")
    assert current[0] == libvirt.VIR_DOMAIN_SHUTOFF
    assert domain.shutdown.call_count == int(name in ("running", "blocked"))
    assert domain.destroy.call_count == int(name in ("nostate", "paused", "crashed", "suspended"))


@pytest.mark.parametrize("name,state", _HYPERVISOR_STATES, ids=[s[0] for s in _HYPERVISOR_STATES])
async def test_hypervisor_state_status(tmp_path, name, state):
    r, domain, current = _state_runtime(tmp_path, state)
    status = await r.get_workspace_status("guest")
    expected = {
        "nostate": "unknown", "running": "running", "blocked": "running",
        "paused": "paused", "shutdown": "stopping", "shutoff": "stopped",
        "crashed": "failed", "suspended": "suspended",
    }
    assert status.status == expected[name]
    assert current[0] == state
    domain.create.assert_not_called()
    domain.destroy.assert_not_called()


@pytest.mark.parametrize("state", ["running", "paused"])
async def test_quiesce_interrupted_workspace_stops_exact_owned_guest(builder, state):
    r = builder
    instance_id = str(uuid.uuid4())
    await r.create_workspace(WorkspaceConfig(
        workspace_id=instance_id, image=str(r._resolve_build_base_image.return_value)
    ))
    if state == "paused":
        r._get_domain(instance_id).suspend()
    try:
        result = await r.quiesce_interrupted_workspace(instance_id)
        assert result == "stopped"
        assert r._get_domain(instance_id).state()[0] == libvirt.VIR_DOMAIN_SHUTOFF
        assert r._disk_path(instance_id).exists()
    finally:
        await r.remove_workspace(instance_id)


@pytest.mark.parametrize("corruption", ["metadata", "disk"])
async def test_quiesce_interrupted_workspace_rejects_unverified_identity(
    builder, corruption
):
    import xml.etree.ElementTree as ET

    r = builder
    instance_id = str(uuid.uuid4())
    await r.create_workspace(WorkspaceConfig(
        workspace_id=instance_id, image=str(r._resolve_build_base_image.return_value)
    ))
    domain = r._get_domain(instance_id)
    original = domain.XMLDesc(0)
    root = ET.fromstring(original)
    if corruption == "metadata":
        node = root.find("./metadata/{https://opencuria.dev/libvirt}workspace/{https://opencuria.dev/libvirt}workspace-id")
        node.text = str(uuid.uuid4())
    else:
        root.find("./devices/disk[@device='disk']/source").set("file", "/foreign.qcow2")
    # Redefining a running domain changes only its next-boot definition. Boot
    # the corrupt definition so the recovery's initial live observation sees it.
    domain.destroy()
    domain = r._libvirt_conn().defineXML(ET.tostring(root, encoding="unicode"))
    domain.create()
    try:
        with pytest.raises(RuntimeError, match="ownership is unverified"):
            await r.quiesce_interrupted_workspace(instance_id)
        assert domain.state()[0] == libvirt.VIR_DOMAIN_RUNNING
    finally:
        domain.destroy()
        r._libvirt_conn().defineXML(original)
        await r.remove_workspace(instance_id)


async def test_quiesce_interrupted_workspace_rejects_changed_incarnation(builder):
    r = builder
    instance_id = str(uuid.uuid4())
    await r.create_workspace(WorkspaceConfig(
        workspace_id=instance_id, image=str(r._resolve_build_base_image.return_value)
    ))
    original_stop = r.stop_workspace

    async def change_incarnation(instance):
        await original_stop(instance)
        old = r._disk_path(instance)
        replacement = old.with_suffix(".replacement")
        await asyncio.to_thread(subprocess.run, ["cp", str(old), str(replacement)], check=True)
        replacement.replace(old)

    r.stop_workspace = AsyncMock(side_effect=change_incarnation)
    try:
        with pytest.raises(RuntimeError, match="shutdown could not be verified"):
            await r.quiesce_interrupted_workspace(instance_id)
    finally:
        await r.remove_workspace(instance_id)


@pytest.mark.parametrize("state", ["absent", "partial"])
async def test_quiesce_interrupted_workspace_absence_requires_all_resources_gone(
    builder, state
):
    r = builder
    instance_id = str(uuid.uuid4())
    if state == "partial":
        qcow(r._disk_path(instance_id))
        assert await r.quiesce_interrupted_workspace(instance_id) == "partial"
        assert not await r.workspace_removed(instance_id)
        assert r._disk_path(instance_id).exists()
        r._disk_path(instance_id).unlink()
    else:
        assert await r.quiesce_interrupted_workspace(instance_id) == "absent"


async def test_quiesce_interrupted_workspace_stop_failure_is_not_quiescence(builder):
    r = builder
    instance_id = str(uuid.uuid4())
    await r.create_workspace(WorkspaceConfig(
        workspace_id=instance_id, image=str(r._resolve_build_base_image.return_value)
    ))
    r.stop_workspace = AsyncMock(side_effect=OSError("libvirt connection lost"))
    try:
        with pytest.raises(OSError, match="connection lost"):
            await r.quiesce_interrupted_workspace(instance_id)
        assert r._get_domain(instance_id).state()[0] == libvirt.VIR_DOMAIN_RUNNING
    finally:
        await r.remove_workspace(instance_id)


async def test_partial_interruption_rejects_foreign_consumer(builder):
    r = builder
    instance_id = str(uuid.uuid4())
    disk = r._disk_path(instance_id)
    qcow(disk)
    domain = r._libvirt_conn().defineXML(f'''<domain type="test">
      <name>foreign-consumer</name><memory unit="KiB">131072</memory><vcpu>1</vcpu>
      <os><type>hvm</type></os><devices>
      <disk type="file" device="disk"><source file="{disk}"/>
      <target dev="vda" bus="virtio"/></disk></devices></domain>''')
    try:
        with pytest.raises(RuntimeError, match="physical dependents"):
            await r.quiesce_interrupted_workspace(instance_id)
        assert disk.exists()
        assert r._libvirt_conn().lookupByName("foreign-consumer")
    finally:
        domain.undefine()
        disk.unlink()


async def test_partial_interruption_rejects_incomplete_inventory(builder):
    from src.runtime.inventory import RuntimeInventory

    r = builder
    instance_id = str(uuid.uuid4())
    disk = r._disk_path(instance_id)
    qcow(disk)
    r.inventory = AsyncMock(return_value=RuntimeInventory(errors=["storage offline"]))
    with pytest.raises(RuntimeError, match="Incomplete inventory"):
        await r.quiesce_interrupted_workspace(instance_id)
    assert disk.exists()


@pytest.mark.parametrize("invalid", [
    "cpu-zero", "cpu-negative", "cpu-capacity", "memory-zero", "memory-negative",
    "disk-zero", "disk-negative",
])
@pytest.mark.parametrize("activation", ["create", "clone"])
async def test_invalid_initial_resources_have_no_effects(builder, activation, invalid):
    r = builder
    instance_id = str(uuid.uuid4())
    resource, value = invalid.split("-")
    values = {"zero": 0, "negative": -1, "capacity": 9}
    resources = {
        {"cpu": "qemu_vcpus", "memory": "qemu_memory_mb", "disk": "qemu_disk_size_gb"}[
            resource
        ]: values[value]
    }
    with pytest.raises(ValueError, match="positive|logical CPU capacity"):
        if activation == "create":
            await r.create_workspace(WorkspaceConfig(
                workspace_id=instance_id,
                image=str(r._resolve_build_base_image.return_value),
                **resources,
            ))
        else:
            await r.create_workspace_from_image_artifact(
                "not-needed-before-validation", instance_id, **resources
            )
    assert list(r._disk_dir.iterdir()) == []
    assert r._libvirt_conn().listAllDomains(0) == []
    assert not any(
        network.name().startswith("opencuria-ws-")
        for network in r._libvirt_conn().listAllNetworks(0)
    )


async def test_restarted_partial_create_can_be_recovered_and_removed(
    builder, tmp_path, monkeypatch
):
    from src.journal import Journal, OperationExecutor
    from src.service import WorkspaceService
    from tests.test_automatic_operation_recovery import Socket, command

    r = builder
    workspace_id = str(uuid.uuid4())
    data = command(target=workspace_id)
    state_dir = tmp_path / "journal"
    journal = Journal(str(state_dir), defer_recovery=True)
    journal.begin("task:create_workspace", data)
    qcow(r._disk_path(workspace_id))
    journal.close()
    journal = Journal(str(state_dir), defer_recovery=True)
    service = WorkspaceService(r, RunnerSettings(state_dir=str(state_dir)))
    service._journal = journal
    handler = AsyncMock()
    executor = OperationExecutor(
        Socket(handler), journal, observer=service,
        reconciler=service.publication_evidence,
    )
    monkeypatch.setattr(executor, "helpers_active", lambda: False)
    service.lifecycle._confirm_workspace_ended = AsyncMock()
    try:
        await executor.replay()
        evidence = await executor.inspect(data)
        assert evidence["quiescent"] and evidence["execution_finished"]
        assert evidence["workspace_status"] == "unknown"
        assert evidence["outcome_known"] is False
        assert "credentials_present" not in evidence
        handler.assert_not_awaited()
        assert r._disk_path(workspace_id).exists()
        await service.lifecycle.remove_workspace(uuid.UUID(workspace_id))
        assert await r.workspace_removed(workspace_id)
    finally:
        journal.close()


async def test_unowned_partial_create_cannot_be_removed(builder, tmp_path):
    from src.journal import Journal
    from src.service import WorkspaceService

    r = builder
    workspace_id = str(uuid.uuid4())
    disk = r._disk_path(workspace_id)
    qcow(disk)
    journal = Journal(str(tmp_path / "journal"), defer_recovery=True)
    service = WorkspaceService(r, RunnerSettings(state_dir=str(tmp_path / "journal")))
    service._journal = journal
    try:
        with pytest.raises(RuntimeError, match="physical registry identity"):
            await service.lifecycle.remove_workspace(uuid.UUID(workspace_id))
        assert disk.exists()
        assert not service.registry.workspace_exists(uuid.UUID(workspace_id))
    finally:
        journal.close()


async def test_owned_partial_create_removal_retries_failed_observation(builder, tmp_path):
    from src.journal import Journal
    from src.service import WorkspaceService
    from tests.test_automatic_operation_recovery import command

    r = builder
    workspace_id = str(uuid.uuid4())
    data = command(target=workspace_id)
    disk = r._disk_path(workspace_id)
    qcow(disk)
    journal = Journal(str(tmp_path / "journal"), defer_recovery=True)
    journal.begin("task:create_workspace", data)
    service = WorkspaceService(r, RunnerSettings(state_dir=str(tmp_path / "journal")))
    service._journal = journal
    service.lifecycle._confirm_workspace_ended = AsyncMock()
    quiesce = r.quiesce_interrupted_workspace
    r.quiesce_interrupted_workspace = AsyncMock(side_effect=OSError("libvirt offline"))
    try:
        with pytest.raises(OSError, match="libvirt offline"):
            await service.lifecycle.remove_workspace(uuid.UUID(workspace_id))
        assert disk.exists()
        assert not service.registry.workspace_exists(uuid.UUID(workspace_id))
        r.quiesce_interrupted_workspace = quiesce
        await service.lifecycle.remove_workspace(uuid.UUID(workspace_id))
        assert await r.workspace_removed(workspace_id)
    finally:
        journal.close()


async def test_owned_guest_missing_registry_can_be_removed(builder, tmp_path):
    from src.journal import Journal
    from src.service import WorkspaceService
    from tests.test_automatic_operation_recovery import command

    r = builder
    workspace_id = str(uuid.uuid4())
    await r.create_workspace(WorkspaceConfig(
        workspace_id=workspace_id, image=str(r._resolve_build_base_image.return_value)
    ))
    journal = Journal(str(tmp_path / "journal"), defer_recovery=True)
    journal.begin("task:create_workspace", command(target=workspace_id))
    service = WorkspaceService(r, RunnerSettings(state_dir=str(tmp_path / "journal")))
    service._journal = journal
    service.lifecycle._confirm_workspace_ended = AsyncMock()
    try:
        assert not service.registry.workspace_exists(uuid.UUID(workspace_id))
        await service.lifecycle.remove_workspace(uuid.UUID(workspace_id))
        assert await r.workspace_removed(workspace_id)
    finally:
        journal.close()


def _invalidate_marker(path, corruption):
    marker = path.with_suffix(".manifest.json")
    if corruption == "json":
        marker.write_text("{invalid JSON")
    else:
        manifest = json.loads(marker.read_text())
        marker.write_text(json.dumps({**manifest, "sha256": "wrong"}))


@pytest.mark.parametrize("corruption", ["hash", "json"])
async def test_invalid_publication_does_not_block_unrelated_workspace_removal(
    builder, corruption
):
    r = builder
    workspace_id = str(uuid.uuid4())
    base = r._resolve_build_base_image.return_value
    await r.create_workspace(WorkspaceConfig(workspace_id=workspace_id, image=str(base)))
    target = r._snapshot_dir / "damaged.qcow2"
    await r._publish_image(base, target, {"operation_id": "publication"})
    _invalidate_marker(target, corruption)
    scan = await r.inventory()
    assert scan.complete, scan.errors
    damaged = next(resource for resource in scan.resources if resource.resource_id == str(target))
    assert damaged.state == "unknown"
    assert damaged.metadata["diagnostic_code"] == "invalid_publication"
    with pytest.raises(RuntimeError, match="Unknown image"):
        await r.delete_image_artifact("damaged")
    with pytest.raises((RuntimeError, ValueError), match="manifest|JSON|property"):
        await r.create_workspace_from_image_artifact("damaged", str(uuid.uuid4()))
    await r.remove_workspace(workspace_id)
    assert await r.workspace_removed(workspace_id)
    assert target.exists()


@pytest.mark.parametrize("corruption", ["hash", "json"])
async def test_invalid_publication_still_fences_its_backing_dependencies(
    builder, corruption
):
    r = builder
    workspace_id = str(uuid.uuid4())
    disk = r._disk_path(workspace_id)
    qcow(disk)
    target = r._snapshot_dir / "damaged.qcow2"
    qcow(target, disk)
    target.with_suffix(".manifest.json").write_text(json.dumps({
        "image_path": str(target), "size_bytes": target.stat().st_size,
        "sha256": r._publication_digest(target),
    }))
    _invalidate_marker(target, corruption)
    scan = await r.inventory()
    assert scan.complete, scan.errors
    damaged = next(resource for resource in scan.resources if resource.resource_id == str(target))
    assert damaged.state == "unknown"
    assert damaged.dependencies == [str(disk)]
    with pytest.raises(RuntimeError, match="physical dependents"):
        await r.remove_workspace(workspace_id)
    assert disk.exists() and target.exists()


async def test_unreadable_published_disk_keeps_dependency_fence(builder):
    r = builder
    workspace_id = str(uuid.uuid4())
    qcow(r._disk_path(workspace_id))
    target = r._snapshot_dir / "damaged.qcow2"
    qcow(target)
    target.with_suffix(".manifest.json").write_text("{invalid JSON")
    with target.open("r+b") as stream:
        stream.truncate(80)
    assert not (await r.inventory()).complete
    with pytest.raises(RuntimeError, match="Incomplete inventory"):
        await r.remove_workspace(workspace_id)
    assert target.exists() and r._disk_path(workspace_id).exists()


async def test_stale_removed_lineage_cannot_delete_reallocated_partial_disk(
    builder, tmp_path
):
    from src.journal import Journal
    from src.service import WorkspaceService
    from tests.test_automatic_operation_recovery import command

    r = builder
    workspace_id = str(uuid.uuid4())
    data = command(target=workspace_id)
    journal = Journal(str(tmp_path / "journal"), defer_recovery=True)
    journal.begin("task:create_workspace", data)
    journal.finish(data["operation_id"], "workspace:created", data)
    removal = command(str(uuid.uuid4()), workspace_id)
    journal.begin("task:remove_workspace", removal)
    journal.finish(removal["operation_id"], "workspace:removed", removal)
    disk = r._disk_path(workspace_id)
    qcow(disk)
    before = disk.stat()
    service = WorkspaceService(r, RunnerSettings(state_dir=str(tmp_path / "journal")))
    service._journal = journal
    r.quiesce_interrupted_workspace = AsyncMock()
    try:
        with pytest.raises(RuntimeError, match="physical registry identity"):
            await service.lifecycle.remove_workspace(uuid.UUID(workspace_id))
        r.quiesce_interrupted_workspace.assert_not_awaited()
        after = disk.stat()
        assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
    finally:
        journal.close()


_OBSERVATION_STATES = [
    ("nostate", libvirt.VIR_DOMAIN_NOSTATE, "unknown"),
    ("running", libvirt.VIR_DOMAIN_RUNNING, "running"),
    ("blocked", libvirt.VIR_DOMAIN_BLOCKED, "running"),
    ("paused", libvirt.VIR_DOMAIN_PAUSED, "paused"),
    ("shutdown", libvirt.VIR_DOMAIN_SHUTDOWN, "stopping"),
    ("shutoff", libvirt.VIR_DOMAIN_SHUTOFF, "stopped"),
    ("crashed", libvirt.VIR_DOMAIN_CRASHED, "failed"),
    ("suspended", libvirt.VIR_DOMAIN_PMSUSPENDED, "suspended"),
    ("unrecognized", 99, "unknown"),
]


def _observed_domain_runtime(tmp_path, state):
    """Supply a readable graph and a libvirt state independent of isActive."""
    r, domain, _ = _state_runtime(tmp_path, state)
    instance_id = str(uuid.uuid4())
    disk = r._disk_path(instance_id)
    qcow(disk)
    domain.name.return_value = r._domain_name(instance_id)
    domain.UUIDString.return_value = "exact-domain"
    domain.XMLDesc.return_value = (
        f'<domain><devices><disk device="disk"><source file="{disk}"/>'
        '</disk></devices></domain>'
    )
    # Even when libvirt says the domain has allocated resources, it need not be
    # able to run commands. The power-state observation must own usability.
    domain.isActive.return_value = True
    r._libvirt_conn.return_value.listAllDomains.return_value = [domain]
    return r, domain, instance_id


@pytest.mark.parametrize(
    "name,state,expected", _OBSERVATION_STATES,
    ids=[row[0] for row in _OBSERVATION_STATES],
)
async def test_listed_hypervisor_state_matches_direct_status(
    tmp_path, name, state, expected
):
    r, domain, instance_id = _observed_domain_runtime(tmp_path, state)
    direct = await r.get_workspace_status(instance_id)
    listed = await r.list_workspaces()
    assert direct.status == expected
    assert len(listed) == 1
    assert listed[0].workspace_id == instance_id
    assert listed[0].status == expected
    domain.create.assert_not_called()
    domain.destroy.assert_not_called()


@pytest.mark.parametrize(
    "name,state,expected", _OBSERVATION_STATES,
    ids=[row[0] for row in _OBSERVATION_STATES],
)
async def test_inventory_hypervisor_state_is_not_inferred_from_activity(
    tmp_path, name, state, expected
):
    from src.service import WorkspaceService
    from tests.test_automatic_operation_recovery import command

    r, domain, instance_id = _observed_domain_runtime(tmp_path, state)
    scan = await r.inventory()
    assert scan.complete, scan.errors
    guest = next(resource for resource in scan.resources if resource.kind == "workspace")
    assert guest.state == ("exited" if expected == "stopped" else expected)
    service = WorkspaceService(r, RunnerSettings(state_dir=str(tmp_path / "journal")))
    evidence = await service.operation_workspace_evidence(
        command(target=instance_id), await service.inventory_for_runner()
    )
    assert evidence["workspace_status"] == {
        "running": "running", "stopped": "stopped",
    }.get(expected, "unknown")
    assert "credentials_present" not in evidence
    domain.create.assert_not_called()
    domain.destroy.assert_not_called()


async def test_fresh_runtime_initializes_cache_before_first_inventory(
    tmp_path, monkeypatch
):
    from unittest.mock import MagicMock

    from src.runtime.qemu_runtime import QemuRuntime

    cache = tmp_path / "fresh" / "image-cache"
    settings = RunnerSettings(
        _env_file=None,
        qemu_disk_dir=str(tmp_path / "disks"),
        qemu_snapshot_dir=str(tmp_path / "snapshots"),
        qemu_image_cache_dir=str(cache),
        qemu_ssh_key_path=str(tmp_path / "ssh" / "runner-key"),
    )
    assert not cache.exists()
    monkeypatch.setattr(QemuRuntime, "_ensure_ssh_keypair", MagicMock())
    runtime = QemuRuntime(settings)
    runtime._build_image_dir = tmp_path / "base-images"
    runtime._libvirt_conn = MagicMock()
    runtime._libvirt_conn.return_value.listAllDomains.return_value = []
    runtime._resolve_build_base_image = MagicMock()
    scan = await runtime.inventory()
    assert cache.is_dir() and cache.stat().st_mode & 0o777 == 0o755
    assert scan.complete, scan.errors
    assert scan.resources == []
    runtime._resolve_build_base_image.assert_not_called()


def test_fresh_runtime_cache_permission_failure_is_explicit(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from src.runtime.qemu_runtime import QemuRuntime

    cache = tmp_path / "image-cache"
    settings = RunnerSettings(
        _env_file=None,
        qemu_disk_dir=str(tmp_path / "disks"),
        qemu_snapshot_dir=str(tmp_path / "snapshots"),
        qemu_image_cache_dir=str(cache),
    )
    mkdir = Path.mkdir

    def denied(path, *args, **kwargs):
        if path == cache:
            raise PermissionError("read-only cache root")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)
    keypair = MagicMock()
    monkeypatch.setattr(QemuRuntime, "_ensure_ssh_keypair", keypair)
    with pytest.raises(RuntimeError, match="Missing permissions.*image-cache"):
        QemuRuntime(settings)
    keypair.assert_not_called()
