"""Opt-in lifecycle fault matrix using real local QEMU/KVM guests.

Run with the runner virtualenv from the repository root. All writable disks,
publications, journals and logs live below a fresh --output directory. Only
domains/networks allocated by this run are removed; existing guests are untouched.
This deliberately does not connect to a deployed backend or runner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.runtime.base import WorkspaceConfig
from src.runtime.qemu_runtime import QemuRuntime
from src.service import WorkspaceService


async def settled(*operations: Awaitable[Any]) -> list[Any]:
    """Join every issued effect before reporting a failed parallel scenario."""
    results = await asyncio.gather(*operations, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def runtime_at(root: Path, state: str = "state") -> tuple[QemuRuntime, RunnerSettings]:
    """Construct an isolated runtime, retaining production VM/SSH code paths."""
    settings = RunnerSettings(
        _env_file=None,
        state_dir=str(root / state),
        qemu_disk_dir=str(root / "disks"),
        qemu_snapshot_dir=str(root / "snapshots"),
        qemu_image_cache_dir=str(root / "cache"),
        qemu_ssh_key_path=str(root / "ssh/key"),
        qemu_ssh_timeout=180,
        qemu_vcpus=1,
        qemu_memory_mb=1024,
        qemu_disk_size_gb=6,
        qemu_build_vcpus=1,
        qemu_build_memory_mb=1024,
        qemu_build_concurrency=2,
    )
    runtime = QemuRuntime(settings)
    runtime._build_image_dir = root / "builds"
    runtime._build_image_dir.mkdir(exist_ok=True)
    return runtime, settings


def command(workspace_id: str, image: str) -> dict[str, Any]:
    """Return one durable create identity which can be delivered repeatedly."""
    task_id = str(uuid.uuid4())
    return {
        "task_id": task_id,
        "operation_id": task_id,
        "attempt": 1,
        "target": workspace_id,
        "workspace_id": workspace_id,
        "runner_id": str(uuid.uuid4()),
        "base_image_path": image,
        "qemu_vcpus": 1,
        "qemu_memory_mb": 1024,
        "qemu_disk_size_gb": 6,
        "repos": [],
    }


async def crash_child(root: Path) -> None:
    """Pause at the actual initialization boundary until the parent SIGKILLs us."""
    runtime, settings = runtime_at(root, "crash-state")
    service = WorkspaceService(runtime, settings)
    interface = WebSocketInterface(service, settings)

    async def offline(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("intentional backend outage")

    async def pause_initialization(*args: Any, **kwargs: Any) -> bool:
        (root / "child-ready").write_text("guest booted; initialization unfinished")
        await asyncio.Event().wait()
        return False

    interface._operations.emit = offline
    service.credentials.inject_workspace_credentials = pause_initialization
    data = json.loads((root / "crash-command.json").read_text())
    await interface._sio.handlers["/"]["task:create_workspace"](data)


async def verify(root: Path, cloud_image: Path, distro: str) -> None:
    """Exercise builds, cloning, resource changes, capture and process failure."""
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o755)
    if any(not parent.stat().st_mode & 0o001 for parent in root.parents):
        raise PermissionError("Choose an output parent traversable by libvirt-qemu")
    (root / "cache").mkdir()
    cache_image = root / "cache" / f"ubuntu-{distro}-server-cloudimg-amd64.img"
    # Preserve sparsity/reflinks when supported; never modify the source fixture.
    await asyncio.to_thread(
        subprocess.run,
        ["cp", "--reflink=auto", "--sparse=always", str(cloud_image), str(cache_image)],
        check=True,
    )
    cache_image.chmod(0o644)
    runtime, settings = runtime_at(root)
    service = WorkspaceService(runtime, settings)
    interface = WebSocketInterface(service, settings)
    emitted: list[tuple[str, Any]] = []
    backend_online = False

    async def transport(event: str, data: Any = None, **kwargs: Any) -> None:
        if not backend_online:
            raise ConnectionError("intentional backend outage")
        emitted.append((event, data))

    interface._operations.emit = transport
    workspaces: set[str] = set()
    build_instances: set[str] = set()
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {"distro": distro, "checks": checks, "passed": False}
    original_prepare = runtime._prepare_build_instance

    async def tracked_prepare(instance_id: str, image: Path) -> Path:
        build_instances.add(instance_id)
        return await original_prepare(instance_id, image)

    runtime._prepare_build_instance = tracked_prepare

    def passed(name: str, **evidence: Any) -> None:
        checks.append({"name": name, **evidence})
        (root / "report.json").write_text(json.dumps(report, indent=2))

    async def guest(instance_id: str, script: str) -> str:
        code, output = await runtime.exec_command_wait(
            instance_id, ["bash", "-lc", script]
        )
        assert code == 0, (code, output)
        return output.strip()

    child = None
    restarted = None
    try:
        image_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        started = time.monotonic()
        results = await settled(
            *(
                runtime.build_image(
                    base_distro=f"ubuntu:{distro}",
                    init_script=(
                        "#!/bin/bash\nset -euo pipefail\n"
                        "mkdir -p /workspace\n"
                        f"echo build-{number} >/workspace/build-marker\n"
                        "dd if=/dev/urandom of=/workspace/build-payload "
                        "bs=1M count=8 status=none\nsync\n"
                    ),
                    image_path=str(runtime._build_image_dir / f"{image_id}.qcow2"),
                    operation_id=str(uuid.uuid4()),
                    image_instance_id=image_id,
                )
                for number, image_id in enumerate(image_ids)
            )
        )
        images = [Path(item["image_path"]) for item in results]
        for image in images:
            info = await runtime._image_info(image)
            assert not info.get("backing-filename")
            assert image.with_suffix(".manifest.json").exists()
        assert not any(
            await asyncio.gather(
                *(runtime.workspace_exists(item) for item in build_instances)
            )
        )
        passed("two concurrent real image builds", seconds=time.monotonic() - started)

        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        workspaces.update((first, second))
        first_command = command(first, str(images[0]))
        await settled(
            interface._sio.handlers["/"]["task:create_workspace"](first_command),
            service.lifecycle.create_workspace(
                repos=[],
                workspace_id=uuid.UUID(second),
                base_image_path=str(images[1]),
                qemu_vcpus=1,
                qemu_memory_mb=1024,
                qemu_disk_size_gb=6,
            ),
        )
        assert await guest(first, "cat /workspace/build-marker") == "build-0"
        assert await guest(second, "cat /workspace/build-marker") == "build-1"
        pending = await interface._operations.journal.call(
            "pending", first_command["operation_id"]
        )
        assert pending[0][0] == "workspace:created", pending
        backend_online = True
        await interface._operations.replay(first_command["operation_id"])
        result = next(data for event, data in emitted if event == "operation:result")
        assert result["event"] == "workspace:created"
        await interface._operations.acknowledge(first_command)
        assert not await interface._operations.journal.call("pending")
        passed("parallel creates with injected transport outage, result replay and ACK")

        inode = runtime._disk_path(first).stat().st_ino
        await interface._sio.handlers["/"]["task:create_workspace"](first_command)
        assert runtime._disk_path(first).stat().st_ino == inode
        try:
            await runtime.create_workspace(
                WorkspaceConfig(
                    workspace_id=first, image=str(images[0]), qemu_disk_size_gb=6
                )
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate raw create must reject existing storage")
        assert await guest(first, "cat /workspace/build-marker") == "build-0"
        passed("duplicate durable and raw create preserve guest disk", inode=inode)

        await guest(first, "echo persistent >/workspace/persistence-marker")
        await service.lifecycle.stop_workspace(uuid.UUID(first))
        artifact = await service.images.create_image_artifact(
            uuid.UUID(first),
            "Lifecycle verification",
            artifact_id=str(uuid.uuid4()),
            operation_id=str(uuid.uuid4()),
        )
        captured = runtime._managed_image_path(artifact.artifact_id)
        assert not (await runtime._image_info(captured)).get("backing-filename")
        clone = str(uuid.uuid4())
        workspaces.add(clone)
        await settled(
            service.images.create_workspace_from_image_artifact(
                artifact.artifact_id,
                uuid.UUID(clone),
                qemu_vcpus=1,
                qemu_memory_mb=1024,
                qemu_disk_size_gb=7,
            ),
            service.lifecycle.resume_workspace(
                uuid.UUID(first),
                qemu_vcpus=1,
                qemu_memory_mb=1024,
                qemu_disk_size_gb=6,
            ),
        )
        assert await guest(clone, "cat /workspace/persistence-marker") == "persistent"
        assert await guest(first, "cat /workspace/persistence-marker") == "persistent"
        passed("scrubbed capture, parallel clone and resume preserve data")

        try:
            await runtime.delete_image_artifact(artifact.artifact_id)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a backing image in use must not be deleted")
        passed("physical clone dependency blocks image deletion")

        marker = captured.with_suffix(".manifest.json")
        original_marker = marker.read_bytes()
        try:
            manifest = json.loads(original_marker)
            manifest["sha256"] = "0" * 64
            marker.write_text(json.dumps(manifest))
            try:
                await runtime.create_workspace_from_image_artifact(
                    artifact.artifact_id, str(uuid.uuid4())
                )
            except (RuntimeError, ValueError):
                pass
            else:
                raise AssertionError("invalid publication must not clone")
        finally:
            marker.write_bytes(original_marker)
        passed("tampered publication is refused before clone allocation")

        await service.lifecycle.update_workspace_resources(
            uuid.UUID(second),
            qemu_vcpus=2,
            qemu_memory_mb=1152,
            qemu_disk_size_gb=7,
        )
        assert await guest(second, "cat /workspace/build-marker") == "build-1"
        passed("live resource update restarts guest and preserves disk")

        domain = await asyncio.to_thread(runtime._get_domain, second)
        await asyncio.to_thread(domain.suspend)
        # Physical stop must also terminate a paused guest. Scrub proof is not
        # invented for externally paused/stopped guests.
        await runtime.stop_workspace(second)
        assert not await asyncio.to_thread(domain.isActive)
        passed("physical stop handles a paused real guest")

        # Free RAM before the crash scenario, then test source-independent capture.
        await service.lifecycle.remove_workspace(uuid.UUID(first))
        workspaces.discard(first)
        assert await guest(clone, "cat /workspace/persistence-marker") == "persistent"
        await service.lifecycle.remove_workspace(uuid.UUID(clone))
        workspaces.discard(clone)
        await runtime.delete_image_artifact(artifact.artifact_id)
        passed("captured clone outlives origin and image deletes after last consumer")

        crash_id = str(uuid.uuid4())
        workspaces.add(crash_id)
        crash_command = command(crash_id, str(images[0]))
        (root / "crash-command.json").write_text(json.dumps(crash_command))
        with (root / "crash-child.log").open("w") as log:
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).resolve()),
                "--crash-child",
                "--output",
                str(root),
                stdout=log,
                stderr=log,
            )

            async def wait_for_child() -> None:
                while not (root / "child-ready").exists():
                    if child.returncode is not None:
                        raise RuntimeError(
                            "crash child failed; inspect crash-child.log"
                        )
                    await asyncio.sleep(0.1)

            await asyncio.wait_for(wait_for_child(), timeout=240)
            child.send_signal(signal.SIGKILL)
            await child.wait()
        restart_runtime, restart_settings = runtime_at(root, "crash-state")
        restart_service = WorkspaceService(restart_runtime, restart_settings)
        restarted = WebSocketInterface(restart_service, restart_settings)
        restarted._operations.emit = transport
        restarted._sio.connected = backend_online
        await restarted._operations.replay()
        terminal = await restarted._operations.journal.call(
            "pending", crash_command["operation_id"]
        )
        assert terminal[0][0] == "workspace:error", terminal
        assert terminal[0][1]["outcome_known"] is False, terminal
        before = (await restart_runtime.workspace_incarnation(crash_id))[0]
        await restarted._sio.handlers["/"]["task:create_workspace"](crash_command)
        after = (await restart_runtime.workspace_incarnation(crash_id))[0]
        assert before == after
        inspection = await restarted._operations.inspect(crash_command)
        assert inspection["execution_finished"] and inspection["quiescent"], inspection
        await restart_runtime.start_workspace(crash_id)
        assert await guest(crash_id, "cat /workspace/build-marker") == "build-0"
        passed(
            "SIGKILL during real guest initialization preserves guest and fences replay"
        )
        report["passed"] = True
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
        cleanup_errors = []
        for instance_id in workspaces | build_instances:
            try:
                await runtime.remove_workspace(instance_id)
            except Exception as error:  # noqa: BLE001 - clean every owned resource
                cleanup_errors.append(f"{instance_id}: {error}")
        if restarted is not None:
            restarted._operations.journal.close()
        interface._operations.journal.close()
        report["cleanup_errors"] = cleanup_errors
        (root / "report.json").write_text(json.dumps(report, indent=2))
        if cleanup_errors:
            raise RuntimeError(f"test resource cleanup failed: {cleanup_errors}")


def main() -> None:
    """Parse the explicit local integration-test configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cloud-image", type=Path)
    parser.add_argument("--distro", default="24.04", choices=("22.04", "24.04"))
    parser.add_argument("--crash-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.crash_child:
        asyncio.run(crash_child(args.output.resolve()))
    else:
        if not args.cloud_image:
            parser.error("--cloud-image is required")
        asyncio.run(
            verify(args.output.resolve(), args.cloud_image.resolve(), args.distro)
        )


if __name__ == "__main__":
    main()
