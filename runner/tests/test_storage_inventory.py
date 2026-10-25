"""Real qcow2 publication and backing graph tests; mocked hypervisor only."""

import asyncio
import subprocess
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import libvirt  # noqa: F401
except ImportError:
    libvirt = types.ModuleType("libvirt")
    libvirt.VIR_DOMAIN_SHUTOFF = 5
    libvirt.VIR_DOMAIN_RUNNING = 1
    libvirt.VIR_DOMAIN_PAUSED = 3
    libvirt.VIR_DOMAIN_UNDEFINE_NVRAM = 4
    libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE = 2
    libvirt.VIR_ERR_NO_DOMAIN = 42

    class LibvirtError(Exception):
        def get_error_code(self):
            return None

    libvirt.libvirtError = LibvirtError
    sys.modules["libvirt"] = libvirt

from src.runtime.qemu_runtime import QemuRuntime, libvirt
from src.runtime.storage import storage_mutation


def runtime(tmp_path):
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


@pytest.mark.asyncio
async def test_capture_self_contained_stopped_scrubbed_and_exclusive(tmp_path):
    r = runtime(tmp_path)
    source = r._snapshot_dir / "legacy.qcow2"
    qcow(source)
    source.with_suffix(".meta").write_text("snapshot_id=legacy\n")
    disk = r._disk_dir / "workspace.qcow2"
    qcow(disk, source)
    domain = MagicMock()
    r._get_domain = lambda _: domain
    domain.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 0)
    image_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="shut off"):
        await r.create_image_artifact(
            "workspace",
            "quoted\n=name",
            artifact_id=image_id,
            operation_id="op",
            credential_clean=True,
        )
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    with pytest.raises(RuntimeError, match="scrub"):
        await r.create_image_artifact(
            "workspace", "name", artifact_id=image_id, operation_id="op"
        )
    artifact = await r.create_image_artifact(
        "workspace",
        "quoted\n=name",
        artifact_id=image_id,
        operation_id="op",
        credential_clean=True,
    )
    target = r._snapshot_dir / (image_id + ".qcow2")
    assert not (await r._image_info(target)).get("backing-filename")
    assert artifact.name == "quoted\n=name"
    with pytest.raises(FileExistsError):
        await r.create_image_artifact(
            "workspace",
            "name",
            artifact_id=image_id,
            operation_id="op",
            credential_clean=True,
        )
    scan = await r.inventory()
    assert scan.complete
    published = next(item for item in scan.resources if item.resource_id == str(target))
    assert published.state == "ready"
    assert published.metadata["name"] == artifact.name


@pytest.mark.asyncio
async def test_capture_descendant_and_unreadable_scan_block_delete(tmp_path):
    r = runtime(tmp_path)
    source = r._snapshot_dir / "parent.qcow2"
    qcow(source)
    source.with_suffix(".meta").write_text("snapshot_id=parent")
    child = r._snapshot_dir / "child.qcow2"
    qcow(child, source)
    child.with_suffix(".meta").write_text("snapshot_id=child")
    with pytest.raises(RuntimeError, match="dependents"):
        await r.delete_image_artifact("parent")
    child.unlink()
    bad = r._disk_dir / "broken.qcow2"
    bad.write_bytes(b"broken")  # qemu info may treat raw, force an actual failure
    r._libvirt_conn.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="Incomplete"):
        await r.delete_image_artifact("parent")
    assert source.exists()


@pytest.mark.asyncio
async def test_publication_crash_marker_enospc_and_invalid_path(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    disk = r._disk_dir / "workspace.qcow2"
    qcow(disk)
    target = r._snapshot_dir / "target.qcow2"
    import os

    original = os.fsync
    calls = 0

    def fail(fd):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("ENOSPC")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        await r._publish_image(disk, target, {"operation_id": "op"})
    assert target.exists()
    assert not target.with_suffix(".manifest.json").exists()
    scan = await r.inventory()
    assert (
        next(i for i in scan.resources if i.resource_id == str(target)).state
        == "unknown"
    )
    with pytest.raises(RuntimeError, match="Unknown"):
        await r.delete_image_artifact("target")
    for reference in ("../escape", "/tmp/foreign.qcow2", "x/y"):
        with pytest.raises(ValueError):
            r._managed_image_path(reference)
    link = r._snapshot_dir / "link.qcow2"
    link.symlink_to(disk)
    with pytest.raises(ValueError):
        r._managed_image_path(str(link))


@pytest.mark.asyncio
async def test_failed_remove_retains_disk(tmp_path):
    r = runtime(tmp_path)
    r._ssh_states = {}
    disk = r._disk_dir / "workspace.qcow2"
    qcow(disk)
    dom = MagicMock()
    dom.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 0)
    dom.destroy.side_effect = libvirt.libvirtError("failure")
    r._get_domain = lambda _: dom
    with pytest.raises(RuntimeError, match="retained"):
        await r.remove_workspace("workspace")
    assert disk.exists()


@pytest.mark.asyncio
async def test_mutation_cancellation_drains_thread_and_holds_lock():
    import threading

    entered, release = threading.Event(), threading.Event()

    class Runtime:
        @storage_mutation
        async def change(self):
            def effect():
                entered.set()
                release.wait()

            await asyncio.to_thread(effect)

    r = Runtime()
    first = asyncio.create_task(r.change())
    await asyncio.to_thread(entered.wait)
    first.cancel()
    second = asyncio.create_task(r.change())
    await asyncio.sleep(0.02)
    assert not first.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second




@pytest.mark.asyncio
async def test_journal_recovers_exact_publication_without_rerun(tmp_path):
    from src.config import RunnerSettings
    from src.journal import COMMANDS, Journal, OperationExecutor
    from src.service import WorkspaceService

    r = runtime(tmp_path)
    source = r._disk_dir / "workspace.qcow2"
    qcow(source)
    image_id = str(uuid.uuid4())
    target = r._snapshot_dir / (image_id + ".qcow2")
    identity = {
        "task_id": "op",
        "operation_id": "op",
        "attempt": 1,
        "target": image_id,
        "runner_id": "runner",
        "image_instance_id": image_id,
        "build_job_id": "job",
        "image_path": str(target),
    }
    journal = Journal(str(tmp_path / "journal"), defer_recovery=True)
    journal.begin("task:build_image", identity)
    await r._publish_image(
        source, target, {"operation_id": "op", "artifact_id": image_id, "kind": "build"}
    )
    journal.close()
    service = WorkspaceService(r, RunnerSettings())

    class Socket:
        def __init__(self):
            from unittest.mock import AsyncMock

            self.handlers = {"/": {event: AsyncMock() for event in COMMANDS}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    socket = Socket()
    reopened = Journal(str(tmp_path / "journal"), defer_recovery=True)
    executor = OperationExecutor(socket, reopened, service.publication_evidence)
    await executor.replay()
    event, result = reopened.pending()[0]
    assert event == "image:built" and result["image_path"] == str(target)
    await socket.handlers["/"]["task:build_image"](identity)
    assert reopened.pending()[0][0] == "image:built"
    reopened.close()


@pytest.mark.asyncio
async def test_explicit_partial_create_removal_preserves_unrelated_disk(tmp_path):
    r = runtime(tmp_path)
    r._ssh_states = {}
    r._host_key_cache = {}
    r._host_key_file = lambda instance: tmp_path / (instance + ".pub")
    r._destroy_workspace_network = AsyncMock()
    r._get_domain = lambda _: (_ for _ in ()).throw(RuntimeError("not defined"))
    workspace_id = str(uuid.uuid4())
    disk = r._disk_dir / (workspace_id + ".qcow2")
    other = r._disk_dir / "other.qcow2"
    qcow(disk)
    qcow(other)
    await r.remove_workspace(workspace_id)
    assert not disk.exists()
    assert other.exists()


def source_domain(r, workspace_id, disk):
    domain = MagicMock()
    domain.name.return_value = r._domain_name(workspace_id)
    domain.UUIDString.return_value = str(uuid.uuid4())
    domain.XMLDesc.return_value = (
        f"<domain><devices><disk device='disk'><source file='{disk}'/>"
        "</disk></devices></domain>"
    )
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    domain.isActive.return_value = False
    domain.isPersistent.return_value = True
    r._libvirt_conn.return_value.listAllDomains.return_value = [domain]
    domain.undefineFlags.side_effect = lambda _: (
        r._libvirt_conn.return_value.listAllDomains.return_value.remove(domain)
    )
    r._get_domain = lambda _: domain
    return domain


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "persistent"),
    [
        (libvirt.VIR_DOMAIN_RUNNING, True),
        (libvirt.VIR_DOMAIN_PAUSED, True),
        (libvirt.VIR_DOMAIN_PAUSED, False),
        (libvirt.VIR_DOMAIN_SHUTOFF, True),
    ],
)
async def test_workspace_remove_confirms_domain_gone_before_cleanup(
    tmp_path: Path, state: int, persistent: bool
) -> None:
    """Paused and transient guests must release their disks before cleanup."""
    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    domain, iso = cloud_init_domain(r, ws, disk)
    domains = r._libvirt_conn.return_value.listAllDomains.return_value
    active = state != libvirt.VIR_DOMAIN_SHUTOFF
    domain.state.return_value = (state, 0)
    domain.isActive.return_value = active
    domain.isPersistent.return_value = persistent
    r._ssh_states = {}
    r._host_key_cache = {ws: "host-key"}
    key = tmp_path / "host-key"
    key.write_text("host-key")
    r._host_key_file = lambda _: key

    def destroy() -> None:
        assert disk.exists() and iso.exists() and key.exists()
        domain.isActive.return_value = False
        if not persistent:
            domains.remove(domain)

    def undefine(_flags: int) -> None:
        assert not domain.isActive()
        assert disk.exists() and iso.exists() and key.exists()
        domains.remove(domain)

    async def remove_network(_instance_id: str) -> None:
        assert not domains
        assert disk.exists() and iso.exists() and key.exists()

    domain.destroy.side_effect = destroy
    domain.undefineFlags.side_effect = undefine
    r._destroy_workspace_network = AsyncMock(side_effect=remove_network)

    await r.remove_workspace(ws)

    assert domain.destroy.call_count == int(active)
    assert domain.undefineFlags.call_count == int(persistent)
    r._destroy_workspace_network.assert_awaited_once_with(ws)
    assert not domains
    assert not disk.exists() and not iso.exists() and not key.exists()
    assert ws not in r._host_key_cache
    assert (await r.inventory()).complete


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["destroy", "undefine", "present", "enumeration"])
async def test_unverified_domain_removal_retains_network_disk_seed_and_key(
    tmp_path: Path, failure: str
) -> None:
    """Failed disposal or absence verification must preserve recovery material."""
    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    domain, iso = cloud_init_domain(r, ws, disk)
    domains = r._libvirt_conn.return_value.listAllDomains.return_value
    domain.state.return_value = (libvirt.VIR_DOMAIN_PAUSED, 0)
    domain.isActive.return_value = True
    r._ssh_states = {}
    r._host_key_cache = {ws: "host-key"}
    key = tmp_path / "host-key"
    key.write_text("host-key")
    r._host_key_file = lambda _: key
    r._destroy_workspace_network = AsyncMock()

    if failure == "destroy":
        domain.destroy.side_effect = libvirt.libvirtError("stop denied")
    elif failure == "undefine":
        domain.undefineFlags.side_effect = libvirt.libvirtError("undefine denied")
    elif failure == "present":
        domain.undefineFlags.side_effect = None
    else:
        r._libvirt_conn.return_value.listAllDomains.side_effect = [
            domains,
            libvirt.libvirtError("enumeration denied"),
        ]

    with pytest.raises(RuntimeError, match="retained"):
        await r.remove_workspace(ws)

    assert disk.exists() and iso.exists() and key.exists()
    assert r._host_key_cache[ws] == "host-key"
    r._destroy_workspace_network.assert_not_awaited()
    if failure == "destroy":
        domain.undefineFlags.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
async def test_paused_workspace_removal_with_libvirt_test_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    """Exercise paused persistent/transient removal with the real libvirt API."""
    if not hasattr(libvirt, "open"):
        pytest.skip("requires libvirt test driver")
    host = tmp_path / "host.xml"
    host.write_text("<node/>")
    try:
        connection = libvirt.open("test://" + str(host))
    except libvirt.libvirtError:
        pytest.skip("requires libvirt test driver")
    try:
        # The test driver has no NVRAM support; destroy/undefine remain real calls.
        monkeypatch.setattr(libvirt, "VIR_DOMAIN_UNDEFINE_NVRAM", 0)
        r = runtime(tmp_path)
        r._libvirt_conn = lambda: connection
        r._ssh_states = {}
        r._host_key_cache = {}
        r._host_key_file = lambda _: tmp_path / "host-key"
        r._destroy_workspace_network = AsyncMock()
        ws = str(uuid.uuid4())
        disk = r._disk_path(ws)
        qcow(disk)
        iso = r._cloud_init_iso_path(ws)
        iso.write_bytes(bytes(2048))
        xml = (
            f'<domain type="test"><name>{r._domain_name(ws)}</name>'
            '<memory unit="KiB">65536</memory><vcpu>1</vcpu>'
            '<os><type>hvm</type></os><devices><disk type="file" device="disk">'
            f'<source file="{disk}"/><target dev="vda" bus="virtio"/></disk>'
            f'<disk type="file" device="cdrom"><source file="{iso}"/>'
            '<target dev="sda" bus="sata"/></disk></devices></domain>'
        )
        if persistent:
            domain = connection.defineXML(xml)
            domain.create()
        else:
            domain = connection.createXML(xml, 0)
        domain.suspend()
        assert domain.state()[0] == libvirt.VIR_DOMAIN_PAUSED
        assert domain.isActive()

        await r.remove_workspace(ws)

        assert not connection.listAllDomains(0)
        assert not disk.exists() and not iso.exists()
        assert (await r.inventory()).complete
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["foreign", "overlay", "partial"])
@pytest.mark.parametrize(
    "state", [libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED]
)
async def test_workspace_remove_checks_graph_before_destroy(
    tmp_path: Path, blocker: str, state: int
) -> None:
    r = runtime(tmp_path)
    workspace_id = str(uuid.uuid4())
    disk = r._disk_path(workspace_id)
    qcow(disk)
    domain = source_domain(r, workspace_id, disk)
    domain.state.return_value = (state, 0)
    domain.isActive.return_value = True
    if blocker == "foreign":
        foreign = MagicMock()
        foreign.name.return_value = "foreign"
        foreign.XMLDesc.return_value = domain.XMLDesc.return_value
        r._libvirt_conn.return_value.listAllDomains.return_value.append(foreign)
    elif blocker == "overlay":
        qcow(r._snapshot_dir / "descendant.qcow2", disk)
    else:
        r._libvirt_conn.return_value.listAllDomains.side_effect = RuntimeError(
            "offline"
        )
    with pytest.raises(RuntimeError, match="dependents|Incomplete"):
        await r.remove_workspace(workspace_id)
    domain.destroy.assert_not_called()
    domain.undefineFlags.assert_not_called()
    assert disk.exists()


@pytest.mark.asyncio
async def test_workspace_delete_rechecks_after_undefine(tmp_path):
    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    domain = source_domain(r, ws, disk)
    r._ssh_states = {}
    r._destroy_workspace_network = AsyncMock()

    def undefine(_flags: int) -> None:
        r._libvirt_conn.return_value.listAllDomains.return_value.remove(domain)
        qcow(r._snapshot_dir / "late.qcow2", disk)

    domain.undefineFlags.side_effect = undefine
    with pytest.raises(RuntimeError, match="dependents"):
        await r.remove_workspace(ws)
    assert disk.exists()


@pytest.mark.asyncio
async def test_source_scrub_survives_corrupt_output_but_not_external_disk_write(
    tmp_path,
):
    from src.config import RunnerSettings
    from src.journal import Journal
    from src.service import WorkspaceService

    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    source_domain(r, ws, disk)
    service = WorkspaceService(r, RunnerSettings())
    observed = await service.workspace_incarnation(ws)
    assert observed and observed[1] == "exited"
    journal = Journal(str(tmp_path / "journal"))
    journal.checkpoint(
        ws,
        observed[0],
        {"state": "exited", "event": "scrubbed", "credentials_present": False},
    )
    # Failed ENOSPC conversion can leave an unreadable qcow header.
    output = r._snapshot_dir / "capture.tmp"
    qcow(output)
    with output.open("r+b") as stream:
        stream.truncate(80)
    assert not (await r.inventory()).complete
    assert journal.proof(ws, (await service.workspace_incarnation(ws))[0])
    with pytest.raises(RuntimeError, match="Incomplete"):
        await r.remove_workspace(ws)
    journal.close()
    subprocess.run(
        ["qemu-io", "-f", "qcow2", "-c", "write 0 4096", str(disk)],
        check=True,
        capture_output=True,
    )
    journal = Journal(str(tmp_path / "journal"))
    assert journal.proof(ws, (await service.workspace_incarnation(ws))[0]) is None
    journal.close()


@pytest.mark.asyncio
async def test_truncated_conversion_failure_retains_safe_source_evidence(tmp_path):
    from src.config import RunnerSettings
    from src.journal import COMMANDS, Journal, OperationExecutor
    from src.service import WorkspaceService

    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    source_domain(r, ws, disk)
    service = WorkspaceService(r, RunnerSettings())
    journal = Journal(str(tmp_path / "journal"))
    observed = await service.workspace_incarnation(ws)
    journal.checkpoint(
        ws,
        observed[0],
        {"state": "exited", "event": "scrubbed", "credentials_present": False},
    )
    journal.close()

    class Socket:
        def __init__(self):
            self.handlers = {"/": {event: handler for event in COMMANDS}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    async def handler(data):
        output = r._snapshot_dir / "failed-conversion.tmp"
        qcow(output)
        with output.open("r+b") as stream:
            stream.truncate(80)
        await socket.emit("image_artifact:failed", {"error": "No space left on device"})

    socket = Socket()
    executor = OperationExecutor(
        socket, Journal(str(tmp_path / "journal")), observer=service
    )
    await socket.handlers["/"]["task:create_image_artifact"](
        {
            "task_id": "capture",
            "operation_id": "capture",
            "runner_id": "runner",
            "attempt": 1,
            "target": ws,
            "workspace_id": ws,
        }
    )
    _, result = executor.journal.pending()[0]
    assert result["execution_finished"] and result["outcome_known"]
    assert not (await r.inventory()).complete
    executor.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_clean", [False, True])
async def test_image_manager_never_bypasses_latest_scrub_proof(cached_clean):
    from src.services.images import ImageManager

    ws = uuid.uuid4()
    info = SimpleNamespace(credentials_present=not cached_clean, instance_id=str(ws))
    runtime = SimpleNamespace(
        create_image_artifact=AsyncMock(side_effect=RuntimeError("scrub required")),
    )
    manager = ImageManager(get_cached=lambda _: info, get_runtime=lambda _: runtime)
    # Caller/cache booleans cannot replace durable source evidence.
    with pytest.raises(RuntimeError, match="scrub"):
        await manager.create_image_artifact(ws, "capture", credential_clean=True)
    assert runtime.create_image_artifact.call_args.kwargs["credential_clean"] is False
    manager.scrub_proof_hook = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="scrub"):
        await manager.create_image_artifact(ws, "capture")
    manager.scrub_proof_hook.assert_awaited_once_with(ws)
    assert runtime.create_image_artifact.call_args.kwargs["credential_clean"] is False








def test_publication_hash_cache_invalidates_same_size_and_mtime(tmp_path, monkeypatch):
    import os

    from src.runtime import qemu_runtime

    r = runtime(tmp_path)
    disk = r._snapshot_dir / "publication.qcow2"
    disk.write_bytes(b"original")
    original_stat = disk.stat()
    digest = qemu_runtime.sha256_digest
    calls = []

    def counted(stream):
        calls.append(True)
        return digest(stream)

    monkeypatch.setattr(qemu_runtime, "sha256_digest", counted)
    expected = r._publication_digest(disk)
    assert r._publication_digest(disk) == expected
    assert len(calls) == 1
    # Filesystem timestamps may have coarse clock granularity.
    import time

    time.sleep(0.02)
    disk.write_bytes(b"foreign!")
    os.utime(disk, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert r._publication_digest(disk) != expected
    assert len(calls) == 2
    assert disk.read_bytes() == b"foreign!"
    disk.unlink()
    disk.symlink_to(r._disk_dir)
    with pytest.raises(ValueError, match="regular"):
        r._publication_digest(disk)


@pytest.mark.asyncio
async def test_cached_publication_foreign_overwrite_cannot_be_deleted(tmp_path):
    import json
    import time

    r = runtime(tmp_path)
    target = r._snapshot_dir / "verified.qcow2"
    qcow(target)
    digest = r._publication_digest(target)
    target.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "image_path": str(target.absolute()),
                "size_bytes": target.stat().st_size,
                "sha256": digest,
            }
        )
    )
    assert (await r.inventory()).complete
    before = target.stat()
    time.sleep(0.02)
    # Same file length and mtime, different content; ctime must invalidate SHA.
    with target.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"!")
    import os

    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(RuntimeError, match="Incomplete"):
        await r.delete_image_artifact("verified")
    assert target.exists()
    assert target.with_suffix(".manifest.json").exists()


@pytest.mark.asyncio
async def test_legacy_base_and_old_meta_keep_physical_backing_identity(tmp_path):
    r = runtime(tmp_path)
    base = tmp_path / "images" / "legacy.qcow2"
    qcow(base)
    child = r._snapshot_dir / "old-capture.qcow2"
    qcow(child, base)
    child.with_suffix(".meta").write_text("snapshot_id=old-capture\n")
    scan = await r.inventory()
    assert scan.complete
    resources = {item.resource_id: item for item in scan.resources}
    assert resources[str(base)].kind == "disk"
    assert resources[str(base)].state == "unknown"
    assert str(base) in resources[str(base)].aliases
    assert resources[str(child)].kind == "image"
    assert resources[str(child)].state == "legacy"
    assert resources[str(child)].aliases == [str(child), "old-capture"]
    assert resources[str(child)].dependencies == [str(base)]
    assert not resources[str(base)].metadata.get("artifact_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["matching", "mismatched", "missing", "symlink"])
async def test_legacy_snapshot_alias_requires_exact_managed_marker(
    tmp_path: Path, marker: str
) -> None:
    r = runtime(tmp_path)
    snapshot_id = str(uuid.uuid4())
    image = r._snapshot_dir / f"{snapshot_id}.qcow2"
    qcow(image)
    meta_path = image.with_suffix(".meta")
    if marker == "symlink":
        foreign_marker = tmp_path / "foreign.meta"
        foreign_marker.write_text(f"snapshot_id={snapshot_id}\n")
        meta_path.symlink_to(foreign_marker)
    elif marker != "missing":
        recorded_id = snapshot_id if marker == "matching" else str(uuid.uuid4())
        meta_path.write_text(f"snapshot_id={recorded_id}\nname=Legacy capture\n")

    # A copied marker in another storage directory cannot claim the snapshot ID.
    copy = tmp_path / "images" / image.name
    qcow(copy)
    copy.with_suffix(".meta").write_text(f"snapshot_id={snapshot_id}\n")
    disk = r._disk_dir / "workspace.qcow2"
    qcow(disk, image)

    scan = await r.inventory()
    resources = {item.resource_id: item for item in scan.resources}
    assert scan.complete is (marker != "symlink")
    assert (snapshot_id in resources[str(image)].aliases) is (marker == "matching")
    assert snapshot_id not in resources[str(copy)].aliases
    assert "artifact_id" not in resources[str(image)].metadata
    assert resources[str(disk)].dependencies == [str(image)]
    with pytest.raises(RuntimeError, match="Incomplete|dependents"):
        await r.delete_image_artifact(snapshot_id)
    assert image.exists()


def cloud_init_domain(r, workspace_id, disk):
    """Real guest disk XML includes the seed CD-ROM as well as the overlay."""
    domain = source_domain(r, workspace_id, disk)
    iso = r._cloud_init_iso_path(workspace_id)
    iso.write_bytes(b"cloud-init seed" + bytes(2048))
    domain.XMLDesc.return_value = (
        f"<domain><devices><disk device='disk'><source file='{disk}'/>"
        f"</disk><disk device='cdrom'><source file='{iso}'/>"
        "</disk></devices></domain>"
    )
    return domain, iso


@pytest.mark.asyncio
async def test_cloud_init_inventory_exact_ownership_and_orphans(tmp_path):
    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    domain, iso = cloud_init_domain(r, ws, disk)
    arbitrary = r._disk_dir / "unrecognized.iso"
    arbitrary.write_bytes(bytes(2048))
    domain.XMLDesc.return_value = domain.XMLDesc.return_value.replace(
        "</devices>", f"<disk><source file='{arbitrary}'/></disk></devices>"
    )
    orphan = r._cloud_init_iso_path(str(uuid.uuid4()))
    orphan.write_bytes(bytes(2048))
    scan = await r.inventory()
    assert scan.complete
    by_id = {resource.resource_id: resource for resource in scan.resources}
    assert by_id[str(iso)].metadata == {
        "workspace_id": ws,
        "role": "cloud_init",
        "filesystem_id": str(iso.stat().st_dev),
        "file_identity": f"{iso.stat().st_dev}:{iso.stat().st_ino}",
    }
    assert by_id[str(disk)].metadata["workspace_id"] == ws
    assert by_id[str(disk)].metadata["file_identity"] == (
        f"{disk.stat().st_dev}:{disk.stat().st_ino}"
    )
    assert by_id[str(iso)].state == "observed"
    for path in (arbitrary, orphan):
        assert by_id[str(path)].state == "unknown"
        assert "workspace_id" not in by_id[str(path)].metadata
        assert by_id[str(path)].metadata["filesystem_id"] == str(path.stat().st_dev)


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
async def test_cloud_init_shared_consumer_blocks_workspace_unlink(tmp_path, late):
    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    domain, iso = cloud_init_domain(r, ws, disk)
    r._ssh_states = {}
    r._destroy_workspace_network = AsyncMock()
    foreign = MagicMock()
    foreign.name.return_value = "foreign-seed-consumer"
    foreign.XMLDesc.return_value = (
        f"<domain><devices><disk><source file='{iso}'/></disk></devices></domain>"
    )
    domains = r._libvirt_conn.return_value.listAllDomains.return_value
    if late:
        def undefine(_flags: int) -> None:
            domains.remove(domain)
            domains.append(foreign)

        domain.undefineFlags.side_effect = undefine
    else:
        domains.append(foreign)
    with pytest.raises(RuntimeError, match="dependents"):
        await r.remove_workspace(ws)
    assert disk.exists() and iso.exists()
    if not late:
        domain.undefineFlags.assert_not_called()
        domain.destroy.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_capture_survives_origin_disk_and_seed_removal(tmp_path):
    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    domain, iso = cloud_init_domain(r, ws, disk)
    image_id = str(uuid.uuid4())
    await r.create_image_artifact(
        ws,
        "independent",
        artifact_id=image_id,
        operation_id="capture",
        credential_clean=True,
    )
    r._ssh_states = {}
    r._host_key_cache = {}
    r._host_key_file = lambda _: tmp_path / "host-key"
    r._destroy_workspace_network = AsyncMock()
    domain.undefineFlags.side_effect = lambda _: setattr(
        r._libvirt_conn.return_value.listAllDomains, "return_value", []
    )
    await r.remove_workspace(ws)
    assert not disk.exists() and not iso.exists()
    target = r._snapshot_dir / (image_id + ".qcow2")
    assert target.exists()
    assert not (await r._image_info(target)).get("backing-filename")
    subprocess.run(["qemu-img", "check", str(target)], check=True, capture_output=True)
    scan = await r.inventory()
    assert scan.complete
    published = next(x for x in scan.resources if x.resource_id == str(target))
    assert published.state == "ready" and not published.dependencies
    assert published.metadata["artifact_id"] == image_id
    assert published.metadata["filesystem_id"] == str(target.stat().st_dev)
    assert published.metadata["file_identity"] == (
        f"{target.stat().st_dev}:{target.stat().st_ino}"
    )


@pytest.mark.asyncio
async def test_qemu_empty_roots_and_hardlinks_deduplicate_filesystems(
    tmp_path, monkeypatch
):
    import os

    r = runtime(tmp_path)
    statvfs = os.statvfs
    calls = []

    def counted(path):
        calls.append(path)
        return statvfs(path)

    monkeypatch.setattr(os, "statvfs", counted)
    empty = await r.inventory()
    assert empty.complete and len(empty.filesystems) == 1
    assert empty.filesystems[0]["filesystem_id"] == str(r._disk_dir.stat().st_dev)
    assert calls == [r._disk_dir]
    calls.clear()
    disk = r._disk_dir / "workspace.qcow2"
    qcow(disk)
    alias = r._snapshot_dir / "hardlink.qcow2"
    os.link(disk, alias)
    scan = await r.inventory()
    assert scan.complete and len(scan.filesystems) == 1
    assert calls == [r._disk_dir]
    resources = {item.resource_id: item for item in scan.resources}
    assert set(resources) == {str(disk), str(alias)}
    assert resources[str(disk)].metadata == resources[str(alias)].metadata
    assert resources[str(disk)].allocated_bytes == disk.stat().st_blocks * 512
    assert resources[str(disk)].metadata["file_identity"] == (
        f"{disk.stat().st_dev}:{disk.stat().st_ino}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_qemu_external_backing_filesystem_and_unavailable_capacity(
    tmp_path, monkeypatch, nested
):
    import os

    r = runtime(tmp_path)
    directory = r._snapshot_dir / "nested" if nested else tmp_path
    directory.mkdir(exist_ok=True)
    external = directory / "external.qcow2"
    qcow(external)
    disk = r._disk_dir / "workspace.qcow2"
    qcow(disk, external)
    real_stat = Path.stat
    real_statvfs = os.statvfs
    calls = []

    def stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == external:
            return SimpleNamespace(
                st_dev=987654,
                st_mode=result.st_mode,
                st_ino=result.st_ino,
                st_blocks=result.st_blocks,
                st_size=result.st_size,
            )
        return result

    def statvfs(path):
        calls.append(path)
        return real_statvfs(path)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(os, "statvfs", statvfs)
    scan = await r.inventory()
    assert scan.complete
    assert calls == [r._disk_dir, external]
    assert {fs["filesystem_id"] for fs in scan.filesystems} == {
        str(r._disk_dir.stat().st_dev),
        "987654",
    }
    resources = {item.resource_id: item for item in scan.resources}
    assert resources[str(disk)].dependencies == [str(external)]
    assert resources[str(external)].managed is nested
    assert resources[str(external)].metadata["filesystem_id"] == "987654"

    def unavailable(path):
        if path == external:
            raise OSError("unavailable")
        return real_statvfs(path)

    monkeypatch.setattr(os, "statvfs", unavailable)
    scan = await r.inventory()
    assert not scan.complete
    assert len(scan.filesystems) == 1
    assert any("Filesystem inspection failed" in error for error in scan.errors)
