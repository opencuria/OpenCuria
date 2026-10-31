"""Failed image publication releases inventory without touching other images."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests import test_parallel_image_builds
from tests.test_storage_inventory import qcow, runtime

builder = test_parallel_image_builds.builder


@pytest.mark.parametrize("failure", ["convert", "check", "inspect"])
async def test_failed_publication_cleans_owned_outputs_and_unblocks_inventory(
    tmp_path, monkeypatch, failure
):
    r = runtime(tmp_path)
    source = r._disk_path("source")
    qcow(source)
    other = r._snapshot_dir / "other.qcow2"
    await r._publish_image(source, other, {"operation_id": "other"})
    target = r._snapshot_dir / "failed.qcow2"
    original = asyncio.create_subprocess_exec

    async def subprocess(*args, **kwargs):
        if args[:2] == ("qemu-img", failure):
            if failure == "convert":
                Path(args[-1]).write_bytes(b"truncated conversion")
            return SimpleNamespace(
                communicate=AsyncMock(return_value=(b"", b"injected failure")),
                returncode=1,
            )
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "create_subprocess_exec", subprocess)
        if failure == "inspect":
            patch.setattr(r, "_image_info", AsyncMock(side_effect=OSError("inspect")))
        with pytest.raises((RuntimeError, OSError)):
            await r._publish_image(source, target, {"operation_id": "failed"})
    assert not target.exists()
    assert not target.with_suffix(".manifest.json").exists()
    assert not list(r._snapshot_dir.glob("*.tmp-*"))
    assert not list(r._snapshot_dir.glob(".publication-*"))
    scan = await r.inventory()
    assert scan.complete, scan.errors
    await r.delete_image_artifact("other")
    assert not other.exists()
    assert source.exists()
    # The same immutable target can be used again after a known failed attempt.
    await r._publish_image(source, target, {"operation_id": "retry"})
    assert (await r.inventory()).complete


@pytest.mark.parametrize("failed_fsync", [1, 2, 3, 4])
async def test_commit_failure_rolls_back_unpublished_outputs_but_keeps_commit(
    tmp_path, monkeypatch, failed_fsync
):
    r = runtime(tmp_path)
    source = r._disk_path("source")
    qcow(source)
    target = r._snapshot_dir / "target.qcow2"
    original = os.fsync
    calls = 0
    failed_once = False

    def fsync(fd):
        nonlocal calls, failed_once
        if any(
            path.stat().st_size
            for path in r._snapshot_dir.glob(".publication-*/disk.part")
        ):
            calls += 1
        if calls == failed_fsync and not failed_once:
            failed_once = True
            raise OSError("injected fsync failure")
        return original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(OSError, match="injected fsync"):
            await r._publish_image(source, target, {"operation_id": "attempt"})
    assert not list(r._snapshot_dir.glob("*.tmp-*"))
    assert not list(r._snapshot_dir.glob(".publication-*"))
    assert source.exists()
    marker = target.with_suffix(".manifest.json")
    assert target.exists() == marker.exists() == (failed_fsync == 4)
    scan = await r.inventory()
    assert scan.complete, scan.errors
    if marker.exists():
        manifest = json.loads(marker.read_text())
        assert manifest["operation_id"] == "attempt"
        assert manifest["sha256"] == r._publication_digest(target)
        image = next(item for item in scan.resources if item.resource_id == str(target))
        assert image.state == "ready"


async def test_failed_commit_preserves_concurrently_created_target(
    tmp_path, monkeypatch
):
    r = runtime(tmp_path)
    source = r._disk_path("source")
    qcow(source)
    target = r._snapshot_dir / "target.qcow2"
    original = os.link

    def link(source_path, target_path):
        if target_path == target:
            target.write_bytes(b"another publisher owns this file")
        return original(source_path, target_path)

    monkeypatch.setattr(os, "link", link)
    with pytest.raises(FileExistsError):
        await r._publish_image(source, target, {"operation_id": "attempt"})
    assert target.read_bytes() == b"another publisher owns this file"
    assert not list(r._snapshot_dir.glob("*.tmp-*"))
    assert not list(r._snapshot_dir.glob(".publication-*"))
    assert source.exists()


@pytest.mark.parametrize("stage", ["upload", "script"])
async def test_hanging_recipe_fails_cleans_guest_and_releases_build_slot(
    builder, stage
):
    from tests.test_parallel_image_builds import build

    r = builder
    r._settings.qemu_build_script_timeout = 0.025
    r._settings.qemu_build_concurrency = 1
    entered = asyncio.Event()

    async def hang(*args):
        entered.set()
        await asyncio.Event().wait()

    operation = r.put_archive if stage == "upload" else r._stream_ssh_process
    operation.side_effect = hang
    with pytest.raises(RuntimeError, match="execution limit"):
        await asyncio.wait_for(build(r), timeout=5)
    assert entered.is_set()
    assert r._libvirt_conn().listAllDomains(0) == []
    assert list(r._disk_dir.iterdir()) == []
    assert not list(r._build_image_dir.iterdir())
    assert not r._build_semaphore.locked()
    operation.side_effect = None
    r._settings.qemu_build_script_timeout = 5
    result = await asyncio.wait_for(build(r), timeout=10)
    assert Path(result["image_path"]).exists()


@pytest.mark.parametrize("value", [0, -1])
def test_build_script_timeout_rejects_nonpositive_values(value):
    from pydantic import ValidationError
    from src.config import RunnerSettings

    with pytest.raises(ValidationError):
        RunnerSettings(_env_file=None, qemu_build_script_timeout=value)
