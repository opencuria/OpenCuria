"""Concurrent provisioning with real QCOW2/ISO tools and libvirt's test driver."""

import asyncio
import json
import shutil
import uuid
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import libvirt
import pytest

from src.runtime.qemu_runtime import QemuRuntime
from tests.test_storage_inventory import qcow, runtime


@pytest.fixture
def builder(tmp_path, monkeypatch):
    """Use an isolated libvirt driver, never the host's active domains."""
    if not all(shutil.which(tool) for tool in ("qemu-img", "genisoimage")):
        pytest.skip("requires qemu-img and genisoimage")
    connection = libvirt.open("test:///default")
    # The test driver does not emulate UEFI/NVRAM, unlike the QEMU driver.
    undefine = libvirt.virDomain.undefineFlags
    monkeypatch.setattr(
        libvirt.virDomain,
        "undefineFlags",
        lambda domain, flags: undefine(
            domain, flags & ~libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
        ),
    )
    for domain in connection.listAllDomains(0):
        if domain.isActive():
            domain.destroy()
        domain.undefine()
    # Test-driver guests do not consume physical CPUs; host admission tests
    # override this value explicitly to exercise one-core hardware.
    monkeypatch.setattr("src.runtime.qemu_runtime.psutil.cpu_count", lambda: 8)
    r = runtime(tmp_path)
    r._settings = SimpleNamespace(
        qemu_image_cache_dir=str(tmp_path / "images"),
        qemu_disk_size_gb=1,
        qemu_vcpus=1,
        qemu_memory_mb=128,
    )
    r._ssh_states = {}
    r._host_key_cache = {}
    r._libvirt_conn = lambda: connection
    r._get_ssh_public_key = MagicMock(return_value="ssh-ed25519 test")
    base = tmp_path / "images" / "ubuntu.img"
    qcow(base)
    r._resolve_build_base_image = MagicMock(return_value=base)
    r._wait_for_ssh = AsyncMock()
    r._wait_for_cloud_init = AsyncMock()
    r.put_archive = AsyncMock()
    r._stream_ssh_process = AsyncMock()
    yield r
    connection.close()


def build(r, image_id=None):
    """Start a distinct, task-bound publication."""
    image_id = image_id or str(uuid.uuid4())
    return asyncio.create_task(
        r.build_image(
            base_distro="ubuntu:24.04",
            init_script="#!/bin/bash\ntrue\n",
            image_path=str(r._build_image_dir / f"{image_id}.qcow2"),
            operation_id=str(uuid.uuid4()),
            image_instance_id=image_id,
        )
    )


async def test_parallel_builds_have_distinct_networks_and_inventory_stays_available(
    builder,
):
    r = builder
    entered, release = asyncio.Event(), asyncio.Event()
    instances = []

    async def provision(instance_id, command, progress):
        instances.append(instance_id)
        if len(instances) == 2:
            entered.set()
        await release.wait()

    r._stream_ssh_process.side_effect = provision
    first, second = build(r), build(r)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert len(set(instances)) == 2
        networks = [
            ET.fromstring(
                r._libvirt_conn()
                .networkLookupByName(r._workspace_network_name(instance_id))
                .XMLDesc(0)
            )
            for instance_id in instances
        ]
        assert len({n.findtext("name") for n in networks}) == 2
        # The test driver retains the name-less XML. The real network driver
        # allocates bridge names atomically instead of reusing a truncated ID.
        assert all(n.find("bridge").get("name") is None for n in networks)
        assert len({n.find("ip").get("address") for n in networks}) == 2
        for network in networks:
            assert network.find("dns").get("enable") == "yes"
            assert network.find("dns").get("forwardPlainNames") == "yes"
            assert network.findall("dns/forwarder") == []
        scan = await asyncio.wait_for(r.inventory(), 5)
        assert scan.complete, scan.errors
        assert len([i for i in scan.resources if i.kind == "workspace"]) == 2
    finally:
        release.set()
        results = await asyncio.gather(first, second)
    for result in results:
        target = r._managed_image_path(result["image_path"])
        manifest = json.loads(target.with_suffix(".manifest.json").read_text())
        assert manifest["kind"] == "build"
        assert manifest["self_contained"]
        assert manifest["virtual_size_bytes"] == 1024**3
        assert manifest["min_disk_size_gb"] == 1
        assert result["virtual_size_bytes"] == 1024**3
        assert result["min_disk_size_gb"] == 1
        assert not (await r._image_info(target)).get("backing-filename")
    assert r._libvirt_conn().listAllDomains(0) == []
    assert not any(
        n.name().startswith("opencuria-ws-")
        for n in r._libvirt_conn().listAllNetworks(0)
    )
    assert list(r._disk_dir.iterdir()) == []


async def test_one_failed_build_is_cleaned_without_aborting_another(builder):
    r = builder
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def provision(instance_id, command, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        else:
            raise RuntimeError("provisioning failed")

    r._stream_ssh_process.side_effect = provision
    good = build(r)
    await asyncio.wait_for(entered.wait(), 5)
    bad = build(r)
    try:
        with pytest.raises(RuntimeError, match="provisioning failed"):
            await asyncio.wait_for(bad, 10)
        assert len(r._libvirt_conn().listAllDomains(0)) == 1
    finally:
        release.set()
        await good
    assert r._libvirt_conn().listAllDomains(0) == []
    assert list(r._disk_dir.iterdir()) == []


async def test_caller_cancellation_drains_build_and_cleanup_without_global_lock(
    builder,
):
    r = builder
    entered, release = asyncio.Event(), asyncio.Event()

    async def provision(*args):
        entered.set()
        await release.wait()

    r._stream_ssh_process.side_effect = provision
    task = build(r)
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    try:
        scan = await asyncio.wait_for(r.inventory(), 5)
        assert scan.complete
        assert not task.done()
        task.cancel()
        scan = await asyncio.wait_for(r.inventory(), 5)
        assert scan.complete
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert r._libvirt_conn().listAllDomains(0) == []
    assert list(r._disk_dir.iterdir()) == []


def test_backend_build_path_is_managed_without_cache_override(tmp_path):
    r = object.__new__(QemuRuntime)
    r._snapshot_dir = tmp_path / "snapshots"
    target = f"/var/lib/opencuria/base-images/{uuid.uuid4()}.qcow2"
    assert str(r._managed_image_path(target)) == target
    for invalid in (
        "/var/lib/opencuria/base-images/../disks/vm.qcow2",
        "/var/lib/opencuria/base-images-foreign/image.qcow2",
    ):
        with pytest.raises(ValueError):
            r._managed_image_path(invalid)
