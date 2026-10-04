"""Real qcow2 publication and backing graph tests; mocked hypervisor only."""

import asyncio
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.runtime.docker_runtime import DockerRuntime
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
    assert (await r.list_image_artifacts("workspace"))[0].name == artifact.name
    with pytest.raises(FileExistsError):
        await r.create_image_artifact(
            "workspace",
            "name",
            artifact_id=image_id,
            operation_id="op",
            credential_clean=True,
        )
    assert (await r.inventory()).complete


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
    r._ssh_connections = {}
    r._ssh_locks = {}
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
async def test_docker_aliases_one_physical_id_foreign_consumer_blocks():
    r = DockerRuntime()
    client = MagicMock()
    r._client = client
    image = MagicMock()
    image.id = "sha256:physical"
    image.attrs = {
        "RepoTags": ["opencuria:one", "opencuria:two"],
        "Size": 100,
        "Config": {"Labels": {}},
    }
    client.images.list.return_value = [image]
    container = MagicMock()
    container.id = "foreign"
    container.attrs = {"Image": image.id, "Config": {"Labels": {}}}
    client.containers.list.return_value = [container]
    client.api.df.return_value = {"Images": [{"Id": image.id, "SharedSize": 90}]}
    scan = await r.inventory()
    assert scan.complete and len([x for x in scan.resources if x.kind == "image"]) == 1
    assert scan.resources[0].shared_bytes == 90
    with pytest.raises(RuntimeError, match="dependents"):
        await r.delete_image_reference("opencuria:one")
    client.images.remove.assert_not_called()


@pytest.mark.asyncio
async def test_journal_recovers_exact_publication_without_rerun(tmp_path):
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
    service = object.__new__(WorkspaceService)
    service.__dict__["_runtimes"] = {"qemu": r}

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
    r._ssh_connections = {}
    r._ssh_locks = {}
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
    r._libvirt_conn.return_value.listAllDomains.return_value = [domain]
    r._get_domain = lambda _: domain
    return domain


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["foreign", "overlay", "partial"])
async def test_workspace_remove_checks_graph_before_destroy(tmp_path, blocker):
    r = runtime(tmp_path)
    workspace_id = str(uuid.uuid4())
    disk = r._disk_path(workspace_id)
    qcow(disk)
    domain = source_domain(r, workspace_id, disk)
    domain.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 0)
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
    r._ssh_connections = {}
    r._ssh_locks = {}
    r._destroy_workspace_network = AsyncMock()
    domain.undefineFlags.side_effect = lambda _: qcow(
        r._snapshot_dir / "late.qcow2", disk
    )
    with pytest.raises(RuntimeError, match="dependents"):
        await r.remove_workspace(ws)
    assert disk.exists()


@pytest.mark.asyncio
async def test_source_scrub_survives_corrupt_output_but_not_external_disk_write(
    tmp_path,
):
    from src.journal import Journal
    from src.service import WorkspaceService

    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    source_domain(r, ws, disk)
    service = object.__new__(WorkspaceService)
    service.__dict__["_runtimes"] = {"qemu": r}
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
    from src.journal import COMMANDS, Journal, OperationExecutor
    from src.service import WorkspaceService

    r = runtime(tmp_path)
    ws = str(uuid.uuid4())
    disk = r._disk_path(ws)
    qcow(disk)
    source_domain(r, ws, disk)
    service = object.__new__(WorkspaceService)
    service.__dict__["_runtimes"] = {"qemu": r}
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
    info = SimpleNamespace(
        credentials_present=not cached_clean, runtime_type="qemu", instance_id=str(ws)
    )
    runtime = SimpleNamespace(
        supports_image_artifacts=True,
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


@pytest.mark.asyncio
async def test_docker_intermediate_not_publication_and_untagged_checkpoint(tmp_path):
    from src.journal import Journal
    from src.service import WorkspaceService

    generation = str(uuid.uuid4())
    tag = f"opencuria/generations:{generation}"
    r = DockerRuntime()
    client = MagicMock()
    r._client = client
    labels = {"opencuria.image-instance-id": generation, "opencuria.operation-id": "op"}

    def image(physical, tags):
        return SimpleNamespace(
            id=physical,
            attrs={"RepoTags": tags, "Size": 100, "Config": {"Labels": labels}},
        )

    intermediate = image("sha256:intermediate", [])
    final = image("sha256:final", [tag])
    client.images.list.return_value = [intermediate]
    client.containers.list.return_value = []
    client.api.df.return_value = {
        "Images": [{"Id": intermediate.id, "SharedSize": 200, "Size": 250}]
    }
    service = object.__new__(WorkspaceService)
    service.__dict__["_runtimes"] = {"docker": r}
    identity = {
        "operation_id": "op",
        "target": generation,
        "image_instance_id": generation,
        "image_tag": tag,
        "failure_event": "image:build_failed",
    }
    scan = await r.inventory()
    assert scan.complete
    assert scan.resources[0].kind == "build_cache"
    assert scan.resources[0].shared_bytes is None
    assert scan.resources[0].metadata["docker_df_shared_bytes"] == 200
    assert await service.publication_evidence(identity) is None
    client.images.list.return_value = [intermediate, final]
    proof = await service.publication_evidence(identity)
    assert proof[1]["image_id"] == final.id
    journal = Journal(str(tmp_path / "journal"))
    journal.publish_image(
        final.id, {"generation_id": generation, "operation_id": "op", "image_tag": tag}
    )
    journal.close()
    reopened = Journal(str(tmp_path / "journal"))
    r._publication_journal = reopened
    final.attrs["RepoTags"] = ["foreign:tag"]
    scan = await r.inventory()
    assert [x.kind for x in scan.resources] == ["build_cache", "image"]
    assert (await service.publication_evidence(identity))[1]["image_id"] == final.id
    with pytest.raises(RuntimeError, match="Foreign/shared"):
        await r.delete_image_reference(final.id)
    reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
async def test_docker_filesystem_capacity_local_only(tmp_path, remote):
    r = DockerRuntime("tcp://daemon:2375" if remote else "unix:///docker.sock")
    r._client = MagicMock()
    r._client.images.list.return_value = []
    r._client.containers.list.return_value = []
    r._client.api.df.return_value = {}
    r._client.info.return_value = {"DockerRootDir": str(tmp_path)}
    scan = await r.inventory()
    assert scan.complete
    fs = scan.filesystems[0]
    assert fs["path"] == str(tmp_path)
    if remote:
        assert fs["capacity_bytes"] is None and fs["available_bytes"] is None
    else:
        assert fs["capacity_bytes"] > 0
        assert 0 <= fs["available_bytes"] <= fs["capacity_bytes"]


@pytest.mark.asyncio
async def test_docker_build_checkpoints_verified_final_and_refuses_overwrite(tmp_path):
    from docker.errors import ImageNotFound
    from src.journal import Journal

    r = DockerRuntime()
    r._client = MagicMock()
    generation = str(uuid.uuid4())
    tag = f"opencuria/generations:{generation}"
    final = SimpleNamespace(id="sha256:built")
    r._client.images.get.side_effect = [ImageNotFound("absent"), final]
    r._client.images.build.return_value = (final, [])
    journal = Journal(str(tmp_path / "journal"))
    r._publication_journal = journal
    await r.build_image(
        dockerfile_content="FROM scratch",
        image_tag=tag,
        operation_id="op",
        image_instance_id=generation,
    )
    assert journal.publications()[final.id]["generation_id"] == generation
    r._client.images.get.side_effect = None
    r._client.images.get.return_value = final
    with pytest.raises(FileExistsError):
        await r.build_image(
            dockerfile_content="FROM scratch",
            image_tag=tag,
            operation_id="other",
            image_instance_id=generation,
        )
    assert r._client.images.build.call_count == 1
    # External overwrite cannot replace the durably bound generation.
    r._client.images.list.return_value = [
        SimpleNamespace(
            id="sha256:replacement",
            attrs={
                "RepoTags": [tag],
                "Size": 100,
                "Config": {
                    "Labels": {
                        "opencuria.image-instance-id": generation,
                        "opencuria.operation-id": "op",
                    }
                },
            },
        )
    ]
    r._client.containers.list.return_value = []
    r._client.api.df.return_value = {}
    scan = await r.inventory()
    assert not scan.complete and scan.resources[0].kind == "build_cache"
    journal.close()


def test_publication_hash_cache_invalidates_same_size_and_mtime(tmp_path, monkeypatch):
    import hashlib
    import os

    r = runtime(tmp_path)
    disk = r._snapshot_dir / "publication.qcow2"
    disk.write_bytes(b"original")
    original_stat = disk.stat()
    digest = hashlib.file_digest
    calls = []

    def counted(stream, algorithm):
        calls.append(True)
        return digest(stream, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", counted)
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
    assert resources[str(child)].dependencies == [str(base)]
    assert not resources[str(base)].metadata.get("artifact_id")


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
    r._ssh_connections = {}
    r._ssh_locks = {}
    r._destroy_workspace_network = AsyncMock()
    foreign = MagicMock()
    foreign.name.return_value = "foreign-seed-consumer"
    foreign.XMLDesc.return_value = (
        f"<domain><devices><disk><source file='{iso}'/></disk></devices></domain>"
    )
    domains = r._libvirt_conn.return_value.listAllDomains.return_value
    if late:
        domain.undefineFlags.side_effect = lambda _: domains.append(foreign)
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
    r._ssh_connections = {}
    r._ssh_locks = {}
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
