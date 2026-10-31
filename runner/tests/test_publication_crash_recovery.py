"""Real process death during QCOW conversion and immutable publication commit."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.test_storage_inventory import qcow, runtime


async def killed_publisher(r, tmp_path, phase):
    operation_id, artifact_id = str(uuid.uuid4()), str(uuid.uuid4())
    target = r._snapshot_dir / f"{artifact_id}.qcow2"
    source = r._disk_path("source")
    qcow(source)
    await asyncio.to_thread(
        subprocess.run,
        ["qemu-io", "-c", "write -P 1 0 1M", str(source)],
        check=True,
        capture_output=True,
    )
    ready = tmp_path / "ready.json"
    # Use actual QEMU conversion with a tiny rate limit so SIGKILL reliably
    # interrupts its disk writes. Commit death is gated after marker linking.
    code = f"""
import asyncio, json, os, signal
from pathlib import Path
from unittest.mock import MagicMock
from src.runtime.qemu_runtime import QemuRuntime
r = object.__new__(QemuRuntime)
r._snapshot_dir = Path({str(r._snapshot_dir)!r})
r._disk_dir = Path({str(r._disk_dir)!r})
r._build_image_dir = Path({str(r._build_image_dir)!r})
target = Path({str(target)!r})
ready = Path({str(ready)!r})
original_spawn = asyncio.create_subprocess_exec
async def spawn(*args, **kwargs):
    if args[:2] == ('qemu-img', 'convert') and {phase!r} == 'convert':
        args = (*args[:2], '-r', '1', *args[2:])
        proc = await original_spawn(*args, **kwargs)
        ready.write_text(json.dumps({{'helper_pid': proc.pid}}))
        return proc
    return await original_spawn(*args, **kwargs)
asyncio.create_subprocess_exec = spawn
original_fsync = os.fsync
def fsync(fd):
    if {phase!r} == 'commit' and target.with_suffix('.manifest.json').exists():
        ready.write_text('{{}}')
        signal.pause()
    return original_fsync(fd)
os.fsync = fsync
asyncio.run(r._publish_image(Path({str(source)!r}), target, {{
    'operation_id': {operation_id!r}, 'artifact_id': {artifact_id!r},
}}))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        cwd=str(Path(__file__).resolve().parents[1]),
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stage = r._publication_stage(target, operation_id)
    try:

        async def wait_ready():
            while not ready.exists() or not (stage / "disk.part").exists():
                if process.returncode is not None:
                    _, stderr = await process.communicate()
                    raise AssertionError(stderr.decode())
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_ready(), 10)
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    assert process.returncode == -signal.SIGKILL
    return (
        target,
        stage,
        {
            "operation_id": operation_id,
            "image_instance_id": artifact_id,
            "image_path": str(target),
            "failure_event": "image_artifact:failed",
        },
    )


@pytest.mark.parametrize("phase", ["convert", "commit"])
async def test_sigkill_publication_cleans_exact_staging_and_preserves_commit(
    tmp_path, phase
):
    r = runtime(tmp_path)
    target, stage, identity = await killed_publisher(r, tmp_path, phase)
    if phase == "convert":
        # Also cover a killed converter whose output header is unusable.
        (stage / "disk.part").write_bytes(b"truncated QCOW output")
    assert stage.exists()
    scan = await r.inventory()
    assert scan.complete, scan.errors
    other = r._snapshot_dir / "other.qcow2"
    await r._publish_image(r._disk_path("source"), other, {"operation_id": "other"})
    await r.delete_image_artifact("other")
    assert not other.exists()
    committed_inode = target.stat().st_ino if target.exists() else None
    await r.recover_image_publication(identity)
    assert not stage.exists()
    assert target.exists() == (phase == "commit")
    if phase == "commit":
        assert target.stat().st_ino == committed_inode
        marker = json.loads(target.with_suffix(".manifest.json").read_text())
        assert marker["operation_id"] == identity["operation_id"]
        assert marker["sha256"] == r._publication_digest(target)
    else:
        await r._publish_image(
            r._disk_path("source"),
            target,
            {
                "operation_id": str(uuid.uuid4()),
                "artifact_id": identity["image_instance_id"],
            },
        )
    assert (await r.inventory()).complete


@pytest.mark.parametrize(
    "change", ["inode", "identity", "dependent", "anchor_dependent"]
)
async def test_crash_cleanup_refuses_changed_ownership_or_physical_dependents(
    tmp_path, change
):
    r = runtime(tmp_path)
    artifact_id, operation_id = str(uuid.uuid4()), str(uuid.uuid4())
    target = r._snapshot_dir / f"{artifact_id}.qcow2"
    r._reserve_image_publication(
        target,
        {
            "operation_id": operation_id,
            "artifact_id": artifact_id,
        },
    )
    stage = r._publication_stage(target, operation_id)
    disk = stage / "disk.part"
    qcow(disk)
    if change == "inode":
        disk.unlink()
        qcow(disk)
    elif change == "identity":
        intent_file = stage / "intent.json"
        intent = json.loads(intent_file.read_text())
        intent_file.write_text(
            json.dumps({**intent, "operation_id": str(uuid.uuid4())})
        )
    else:
        qcow(
            r._disk_path("dependent"),
            stage / "disk.part.owner" if change == "anchor_dependent" else disk,
        )
    with pytest.raises(RuntimeError, match="ownership|identity|dependents"):
        await r.recover_image_publication(
            {
                "operation_id": operation_id,
                "image_instance_id": artifact_id,
                "image_path": str(target),
                "failure_event": "image_artifact:failed",
            }
        )
    assert disk.exists() and stage.exists()


@pytest.mark.parametrize(
    "cut", ["mkdir", "intent_write", "outputs_removed", "empty_outputs"]
)
async def test_reservation_and_cleanup_crash_windows_do_not_strand_recovery(
    tmp_path, cut
):
    r = runtime(tmp_path)
    operation_id, artifact_id = str(uuid.uuid4()), str(uuid.uuid4())
    target = r._snapshot_dir / f"{artifact_id}.qcow2"
    stage = r._publication_stage(target, operation_id)
    if cut in {"mkdir", "intent_write"}:
        stage.mkdir()
        if cut == "intent_write":
            (stage / "intent.json").write_text('{"operation_id":')
    else:
        r._reserve_image_publication(
            target,
            {
                "operation_id": operation_id,
                "artifact_id": artifact_id,
            },
        )
        if cut == "outputs_removed":
            for name in (
                "disk.part",
                "manifest.part",
                "disk.part.owner",
                "manifest.part.owner",
            ):
                (stage / name).unlink()
        else:
            intent_path = stage / "intent.json"
            intent = json.loads(intent_path.read_text())
            intent.update(disk_identity=None, marker_identity=None)
            intent_path.write_text(json.dumps(intent))
            (stage / "disk.part.owner").unlink()
            (stage / "manifest.part.owner").unlink()
    identity = {
        "operation_id": operation_id,
        "image_instance_id": artifact_id,
        "image_path": str(target),
        "failure_event": "image_artifact:failed",
    }
    await r.recover_image_publication(identity)
    await r.recover_image_publication(identity)
    assert (await r.inventory()).complete
    assert not target.exists()
    if cut in {"intent_write", "outputs_removed"}:
        # Metadata without proven contents is retained, never guessed/deleted.
        assert (stage / "intent.json").exists()
    else:
        assert not stage.exists()
