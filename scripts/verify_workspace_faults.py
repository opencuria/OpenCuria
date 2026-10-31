"""Opt-in real KVM resource and storage fault checks, V065/67/68/71/72.

Faults are deliberately injected at named boundaries. Only guests and files below
one fresh output root are created or removed; no deployed backend is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

from src.runtime.base import WorkspaceConfig
from src.runtime.qemu_runtime import GuestCapacityUnavailable, libvirt
from verify_workspace_lifecycle import runtime_at


async def verify(root: Path, cloud_image: Path) -> None:
    """Run independently auditable physical faults against the production runtime."""
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o755)
    if any(not parent.stat().st_mode & 0o001 for parent in root.parents):
        raise PermissionError("Choose an output parent traversable by libvirt-qemu")
    runtime, _ = runtime_at(root)
    runtime._resolve_build_base_image = lambda _: cloud_image
    operation_id, image_id = str(uuid.uuid4()), str(uuid.uuid4())
    builder_id = f"image-build-{operation_id}"
    guest_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    report = {"checks": [], "cleanup_errors": [], "passed": False}
    target = runtime._build_image_dir / f"{image_id}.qcow2"
    mounted = False

    def record(case: str, **evidence) -> None:
        report["checks"].append({"id": case, "status": "PASS", **evidence})
        (root / "report.json").write_text(json.dumps(report, indent=2))

    async def guest(instance: str, command: str) -> str:
        code, output = await runtime.exec_command_wait(
            instance, ["bash", "-lc", command]
        )
        if code:
            raise RuntimeError(f"Guest command failed ({code}): {output}")
        return output.strip()

    try:
        # V065: simulate pressure, not a physical host OOM, while actually building.
        release, queued = asyncio.Event(), asyncio.Event()
        original = runtime._assert_build_memory_available
        progress = []

        def capacity() -> None:
            if not release.is_set():
                raise GuestCapacityUnavailable("injected temporary host RAM pressure")
            original()

        async def on_progress(message: str) -> None:
            progress.append(message)
            if "Waiting for host memory" in message:
                queued.set()

        with patch.object(runtime, "_assert_build_memory_available", capacity):
            task = asyncio.create_task(
                runtime.build_image(
                    base_distro="ubuntu:24.04",
                    init_script="#!/bin/bash\nset -euo pipefail\necho fault-matrix > /workspace/fault-marker\nsync\n",
                    image_path=str(target),
                    operation_id=operation_id,
                    image_instance_id=image_id,
                    progress_callback=on_progress,
                )
            )
            try:
                await asyncio.wait_for(queued.wait(), 10)
                assert not await runtime.workspace_exists(builder_id)
                assert not runtime._disk_path(builder_id).exists()
                assert not any(
                    n.name() == runtime._workspace_network_name(builder_id)
                    for n in runtime._libvirt_conn().listAllNetworks(0)
                )
                assert (await asyncio.wait_for(runtime.inventory(), 5)).complete
            finally:
                release.set()
                result = await asyncio.wait_for(task, 600)
        assert Path(result["image_path"]).exists()
        assert not await runtime.workspace_exists(builder_id)
        record(
            "V065",
            operation_id=operation_id,
            progress=progress,
            queued_without_disk_network_domain=True,
            automatic_completion=True,
        )

        # Both guests use the actual queued build output and real cloud-init.
        await asyncio.gather(
            *(
                runtime.create_workspace(
                    WorkspaceConfig(
                        workspace_id=instance,
                        image=str(target),
                        qemu_vcpus=1,
                        qemu_memory_mb=1024,
                        qemu_disk_size_gb=6,
                    )
                )
                for instance in guest_ids
            )
        )
        first, second = guest_ids
        assert await guest(first, "cat /workspace/fault-marker") == "fault-matrix"
        domain = runtime._get_domain(first)
        flags = libvirt.VIR_DOMAIN_AFFECT_LIVE
        await asyncio.to_thread(
            domain.setBlockIoTune, "vda", {"write_bytes_sec": 524288}, flags
        )
        observed = await asyncio.to_thread(domain.blockIoTune, "vda", flags)
        assert observed["write_bytes_sec"] == 524288
        started = time.monotonic()
        responsiveness = {"ticks": 0, "max_gap_seconds": 0.0}
        stop_ticks = asyncio.Event()

        async def ticker() -> None:
            previous = time.monotonic()
            while not stop_ticks.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                responsiveness["ticks"] += 1
                responsiveness["max_gap_seconds"] = max(
                    responsiveness["max_gap_seconds"], now - previous
                )
                previous = now

        async def throttled_write() -> float:
            began = time.monotonic()
            await guest(
                first,
                "dd if=/dev/urandom of=/workspace/throttled-write bs=1M count=4 conv=fsync status=none",
            )
            return time.monotonic() - began

        async def observed_inventory() -> tuple[object, float]:
            began = time.monotonic()
            scan = await runtime.inventory()
            return scan, time.monotonic() - began

        ticks = asyncio.create_task(ticker())
        write = asyncio.create_task(throttled_write())
        inventory = asyncio.create_task(observed_inventory())
        try:
            await asyncio.sleep(0.5)
            assert not write.done(), "write finished before throttle observation"
            latency_start = time.monotonic()

            async def unrelated_guest() -> None:
                assert await guest(second, "printf responsive") == "responsive"
                other = await asyncio.to_thread(runtime._get_domain, second)
                await asyncio.to_thread(other.suspend)
                await runtime.stop_workspace(second)

            await asyncio.wait_for(unrelated_guest(), 5)
            latency = time.monotonic() - latency_start
            assert not write.done(), "unrelated operation did not overlap slow write"
            write_seconds = await asyncio.wait_for(write, 90)
            scan, inventory_seconds = await asyncio.wait_for(inventory, 120)
            assert scan.complete, scan.errors
            assert responsiveness["ticks"] >= 5, responsiveness
            assert responsiveness["max_gap_seconds"] < 1, responsiveness
            assert write_seconds >= 4, write_seconds
        finally:
            await asyncio.gather(write, inventory, return_exceptions=True)
            stop_ticks.set()
            await ticks
            await asyncio.to_thread(
                domain.setBlockIoTune, "vda", {"write_bytes_sec": 0}, flags
            )
        duration = time.monotonic() - started
        assert duration >= 4, duration
        record(
            "V067",
            throttle_bytes_per_second=524288,
            write_bytes=4 * 1024**2,
            seconds=write_seconds,
            total_seconds=duration,
            unrelated_exec_stop_seconds=latency,
            inventory_seconds=inventory_seconds,
            event_loop=responsiveness,
        )

        await asyncio.to_thread(domain.destroy)
        assert (await runtime.get_workspace_status(first)).status == "stopped"
        await runtime.start_workspace(first)
        assert await guest(first, "cat /workspace/fault-marker") == "fault-matrix"
        await runtime.stop_workspace(first)
        assert (await runtime.get_workspace_status(first)).status == "stopped"
        record(
            "V068",
            abrupt_poweroff_detected=True,
            resumed_with_data=True,
            controlled_stop_verified=True,
        )

        # V072 injects libvirt enumeration failure while real guests/disks exist.
        connection = runtime._libvirt_conn()
        before = runtime._disk_path(first).stat()

        class UnavailableInventory:
            def listAllDomains(self, flags):
                raise libvirt.libvirtError("injected libvirt inventory outage")

            def __getattr__(self, name):
                return getattr(connection, name)

        with patch.object(runtime, "_libvirt_conn", lambda: UnavailableInventory()):
            scan = await runtime.inventory()
            assert not scan.complete and scan.errors
            try:
                await runtime.remove_workspace(first)
            except RuntimeError as exc:
                assert "Incomplete" in str(exc)
            else:
                raise AssertionError(
                    "destructive removal accepted incomplete inventory"
                )
            try:
                await runtime.workspace_removed(first)
            except libvirt.libvirtError:
                pass
            else:
                raise AssertionError("absence inspection swallowed libvirt failure")
        after = runtime._disk_path(first).stat()
        assert (before.st_dev, before.st_ino, before.st_size) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
        )
        assert (await runtime.get_workspace_status(first)).status == "stopped"
        record(
            "V072",
            incomplete_errors=scan.errors,
            deletion_refused=True,
            original_disk_unchanged=True,
        )

        # V071 fills a separate 2 MiB tmpfs; no host root filesystem is filled.
        mountpoint = root / "tiny-filesystem"
        mountpoint.mkdir()
        await asyncio.to_thread(
            subprocess.run,
            [
                "mount",
                "-t",
                "tmpfs",
                "-o",
                "size=2m,mode=755",
                "tmpfs",
                str(mountpoint),
            ],
            check=True,
            capture_output=True,
        )
        mounted = True
        isolated = copy.copy(runtime)
        isolated._build_image_dir = mountpoint
        isolated._snapshot_dir = root / "storage-snapshots"
        isolated._snapshot_dir.mkdir()
        cache = root / "storage-cache"
        cache.mkdir()
        isolated._settings = runtime._settings.model_copy(
            update={"qemu_image_cache_dir": str(cache)}
        )
        source = root / "tiny-source.qcow2"
        await asyncio.to_thread(
            subprocess.run,
            ["qemu-img", "create", "-f", "qcow2", str(source), "8M"],
            check=True,
            capture_output=True,
        )
        existing = mountpoint / "existing.qcow2"
        await isolated._publish_image(
            source, existing, {"operation_id": str(uuid.uuid4())}
        )
        digest = await asyncio.to_thread(isolated._publication_digest, existing)
        inode = existing.stat().st_ino
        raw = root / "nonzero.raw"
        await asyncio.to_thread(
            subprocess.run,
            ["dd", "if=/dev/urandom", f"of={raw}", "bs=1M", "count=4", "status=none"],
            check=True,
            capture_output=True,
        )
        large = root / "large-source.qcow2"
        await asyncio.to_thread(
            subprocess.run,
            ["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(raw), str(large)],
            check=True,
            capture_output=True,
        )
        failed = mountpoint / "failed.qcow2"
        try:
            await isolated._publish_image(
                large, failed, {"operation_id": str(uuid.uuid4())}
            )
        except RuntimeError as exc:
            assert "conversion failed" in str(exc)
        else:
            raise AssertionError(
                "oversized conversion unexpectedly fit tiny filesystem"
            )
        assert not failed.exists() and not failed.with_suffix(".manifest.json").exists()
        assert existing.stat().st_ino == inode
        assert await asyncio.to_thread(isolated._publication_digest, existing) == digest
        record(
            "V071",
            filesystem="isolated 2 MiB tmpfs",
            attempted_source_bytes=large.stat().st_size,
            known_publication_unchanged=True,
            failed_publication_not_ready=True,
        )
        await asyncio.to_thread(
            subprocess.run, ["umount", str(mountpoint)], check=True, capture_output=True
        )
        mounted = False
        report["passed"] = True
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if mounted:
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["umount", str(root / "tiny-filesystem")],
                    check=True,
                    capture_output=True,
                )
            except Exception as exc:  # noqa: BLE001 - preserve cleanup evidence
                report["cleanup_errors"].append(f"tmpfs: {exc}")
        for instance in [*guest_ids, builder_id]:
            try:
                await runtime.remove_workspace(instance)
            except Exception as exc:  # noqa: BLE001 - clean every owned resource
                report["cleanup_errors"].append(f"{instance}: {exc}")
        if target.exists():
            try:
                await runtime.delete_image_artifact(str(target))
            except Exception as exc:  # noqa: BLE001 - preserve cleanup evidence
                report["cleanup_errors"].append(f"image: {exc}")
        if report["cleanup_errors"]:
            report["passed"] = False
        (root / "report.json").write_text(json.dumps(report, indent=2))


def main() -> None:
    """Parse isolated local fixtures; reject accidental output reuse."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cloud-image", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.output.resolve(), args.cloud_image.resolve(strict=True)))


if __name__ == "__main__":
    main()
