"""Opt-in real KVM state matrix; see docs/testing/lifecycle-vm-cases.md.

Use the runner virtualenv. A fresh --output directory isolates every resource.
The compact report records assertions individually, never inferring PASS from
another case. Only domains/networks/images allocated by this invocation change.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

from src.interfaces.websocket import WebSocketInterface
from src.runtime.base import WorkspaceConfig
from src.runtime.storage import sha256_digest
from src.service import WorkspaceService
from verify_workspace_lifecycle import command, runtime_at, settled


def digest_image(path: Path) -> str:
    """Hash real image bytes using the project's bounded-memory helper."""
    with path.open("rb") as stream:
        return sha256_digest(stream)


class Matrix:
    """Persist each verified expectation before proceeding to another effect."""

    def __init__(self, root: Path, distro: str) -> None:
        self.root = root
        self.offset = 0 if distro == "22.04" else 32
        self.report: dict[str, Any] = {
            "distro": distro,
            "cases": {},
            "passed": False,
            "faults": "real VMs; injected transport outage and runner SIGKILL",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "source_sha256": {
                name: hashlib.sha256(
                    (Path(__file__).resolve().parents[1] / name).read_bytes()
                ).hexdigest()
                for name in (
                    "runner/src/runtime/qemu_runtime.py",
                    "runner/src/journal.py",
                    "runner/src/service.py",
                    "runner/src/services/workspace_lifecycle.py",
                )
            },
        }

    def write(self) -> None:
        """Record durable, small evidence after every completed assertion."""
        (self.root / "report.json").write_text(json.dumps(self.report, indent=2))

    def passed(self, number: int, **evidence: Any) -> None:
        """Mark only a scenario whose preceding assertions have completed."""
        self.report["cases"][f"V{number + self.offset:03}"] = {
            "status": "PASS",
            **evidence,
        }
        self.write()


async def verify(root: Path, cloud_image: Path, distro: str) -> None:
    """Exercise the predeclared build/workspace/settings/crash combinations."""
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o755)
    if any(not parent.stat().st_mode & 0o001 for parent in root.parents):
        raise PermissionError("Choose an output parent traversable by libvirt-qemu")
    (root / "cache").mkdir()
    cached = root / "cache" / f"ubuntu-{distro}-server-cloudimg-amd64.img"
    await asyncio.to_thread(
        subprocess.run,
        ["cp", "--reflink=auto", "--sparse=always", str(cloud_image), str(cached)],
        check=True,
    )
    cached.chmod(0o644)
    matrix = Matrix(root, distro)
    runtime, settings = runtime_at(root)
    service = WorkspaceService(runtime, settings)
    interface = WebSocketInterface(service, settings)
    owned: set[str] = set()
    images: set[Path] = set()
    active_tasks: list[asyncio.Task] = []
    child = None
    restarted = None
    emissions: list[tuple[str, Any]] = []
    online = False

    async def emit(event: str, data: Any = None, **kwargs: Any) -> None:
        if not online:
            raise ConnectionError("intentional backend outage")
        emissions.append((event, data))

    interface._operations.emit = emit

    async def guest(instance_id: str, script: str) -> str:
        code, output = await runtime.exec_command_wait(
            instance_id, ["bash", "-lc", script]
        )
        assert code == 0, (instance_id, code, output)
        return output.strip()

    async def absent(instance_id: str) -> None:
        assert await runtime.workspace_removed(instance_id), instance_id

    async def bounded(awaitable, seconds: float = 240):
        return await asyncio.wait_for(awaitable, seconds)

    try:
        # Hold both real builders in guest scripts while unrelated operations run.
        operations = [str(uuid.uuid4()) for _ in range(3)]
        image_ids = [str(uuid.uuid4()) for _ in range(3)]
        builders = [runtime._build_instance_id(op) for op in operations]
        owned.update(builders)
        paths = [runtime._build_image_dir / f"{item}.qcow2" for item in image_ids]
        images.update(paths)
        ready = [asyncio.Event() for _ in range(3)]
        queued = asyncio.Event()
        progress: list[list[str]] = [[], [], []]

        async def build(index: int) -> dict:
            async def callback(line: str) -> None:
                progress[index].append(line)
                if "Temporary build VM is ready" in line:
                    ready[index].set()
                if index == 2 and "Waiting" in line:
                    queued.set()

            return await runtime.build_image(
                base_distro=f"ubuntu:{distro}",
                init_script=(
                    "#!/bin/bash\nset -euo pipefail\nmkdir -p /workspace\n"
                    f"echo build-{index} > /workspace/build-marker\n"
                    "for n in $(seq 1 600); do "
                    "[ ! -f /workspace/build-release ] || break; sleep 1; done\n"
                    "test -f /workspace/build-release\nsync\n"
                ),
                image_path=str(paths[index]),
                operation_id=operations[index],
                image_instance_id=image_ids[index],
                progress_callback=callback,
            )

        active_tasks = [asyncio.create_task(build(i)) for i in range(2)]
        await bounded(asyncio.gather(*(event.wait() for event in ready[:2])))
        assert all(
            await asyncio.gather(*(runtime.workspace_exists(i) for i in builders[:2]))
        )
        assert not any(task.done() for task in active_tasks)
        matrix.passed(1, builders=builders[:2])
        active_tasks.append(asyncio.create_task(build(2)))
        await bounded(queued.wait(), 15)
        await absent(builders[2])
        matrix.passed(2, third_builder_absent=True, progress=progress[2])

        mixed = str(uuid.uuid4())
        owned.add(mixed)
        await service.lifecycle.create_workspace(
            repos=[],
            workspace_id=uuid.UUID(mixed),
            base_image_path=str(cached),
            qemu_vcpus=1,
            qemu_memory_mb=1024,
            qemu_disk_size_gb=6,
        )
        await guest(mixed, "echo mixed >/workspace/persist")
        assert not any(task.done() for task in active_tasks[:2])
        matrix.passed(3)
        await service.lifecycle.update_workspace_resources(
            uuid.UUID(mixed),
            qemu_vcpus=1,
            qemu_memory_mb=1152,
            qemu_disk_size_gb=7,
        )
        assert await guest(mixed, "cat /workspace/persist") == "mixed"
        matrix.passed(4)
        await service.lifecycle.stop_workspace(uuid.UUID(mixed))
        assert (await runtime.get_workspace_status(mixed)).status in {
            "exited",
            "stopped",
        }
        assert not any(task.done() for task in active_tasks[:2])
        matrix.passed(5)
        for instance_id in builders[:2]:
            await guest(instance_id, "touch /workspace/build-release")
        await bounded(ready[2].wait())
        await guest(builders[2], "touch /workspace/build-release")
        results = await bounded(settled(*active_tasks), 600)
        active_tasks.clear()
        assert [result["image_path"] for result in results] == [str(p) for p in paths]
        matrix.passed(6, progress=progress)
        for index, path in enumerate(paths):
            info = await runtime._image_info(path)
            assert not info.get("backing-filename")
            manifest = json.loads(path.with_suffix(".manifest.json").read_text())
            assert manifest["operation_id"] == operations[index], manifest
            assert manifest["artifact_id"] == image_ids[index], manifest
            digest = await asyncio.to_thread(digest_image, path)
            assert manifest["sha256"] == digest
            assert results[index]["min_disk_size_gb"] == 6
        matrix.passed(7, results=results)
        inventory = await runtime.inventory()
        assert inventory.complete, inventory.errors
        aliases = {alias for row in inventory.resources for alias in row.aliases}
        assert all(str(path) in aliases for path in paths), aliases
        assert not any(
            await asyncio.gather(*(runtime.workspace_exists(i) for i in builders))
        )
        matrix.passed(8, resources=len(inventory.resources))
        await service.lifecycle.remove_workspace(uuid.UUID(mixed))

        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        owned.update((first, second))
        data = command(first, str(paths[0]))
        data["env_vars"] = {"LIFECYCLE_SENTINEL": "nonsecret-test-value"}
        await interface._sio.handlers["/"]["task:create_workspace"](data)
        assert await guest(first, "cat /workspace/build-marker") == "build-0"
        assert (
            await guest(first, "cloud-init status --wait >/dev/null; echo ready")
            == "ready"
        )
        matrix.passed(9)
        pending = await interface._operations.journal.call(
            "pending", data["operation_id"]
        )
        assert pending and pending[0][0] == "workspace:created", pending
        assert (
            await guest(
                first, "test -f /root/.opencuria-env.sh; echo credential-present"
            )
            == "credential-present"
        )
        matrix.passed(10)
        online = True
        await interface._operations.replay(data["operation_id"])
        terminal = [row for event, row in emissions if event == "operation:result"][-1]
        assert terminal["data"]["operation_id"] == data["operation_id"]
        await interface._operations.acknowledge(data)
        # pending(op_id) deliberately returns acknowledged replay tombstones;
        # the unfiltered queue contains only results still awaiting an ACK.
        assert not await interface._operations.journal.call("pending")
        matrix.passed(11)
        inode = runtime._disk_path(first).stat().st_ino
        await interface._sio.handlers["/"]["task:create_workspace"](data)
        try:
            await runtime.create_workspace(
                WorkspaceConfig(
                    workspace_id=first, image=str(paths[0]), qemu_disk_size_gb=6
                )
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("Raw duplicate create must be refused")
        assert runtime._disk_path(first).stat().st_ino == inode
        assert await guest(first, "cat /workspace/build-marker") == "build-0"
        matrix.passed(12, inode=inode)
        await service.lifecycle.create_workspace(
            repos=[],
            workspace_id=uuid.UUID(second),
            base_image_path=str(paths[1]),
            qemu_vcpus=1,
            qemu_memory_mb=1024,
            qemu_disk_size_gb=6,
        )
        await guest(first, "echo persistent >/workspace/persistence-marker")
        await service.lifecycle.stop_workspace(uuid.UUID(first))
        assert await interface._workspace_scrub_proof(uuid.UUID(first))
        matrix.passed(13)
        artifact = await service.images.create_image_artifact(
            uuid.UUID(first),
            "State matrix",
            artifact_id=str(uuid.uuid4()),
            operation_id=str(uuid.uuid4()),
        )
        capture = runtime._managed_image_path(artifact.artifact_id)
        images.add(capture)
        assert not (await runtime._image_info(capture)).get("backing-filename")
        assert artifact.min_disk_size_gb == 6
        manifest = json.loads(capture.with_suffix(".manifest.json").read_text())
        assert manifest["instance_id"] == first, manifest
        matrix.passed(14, image=artifact.artifact_id)
        clone = str(uuid.uuid4())
        owned.add(clone)
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
        for instance_id in (first, clone):
            assert (
                await guest(instance_id, "cat /workspace/persistence-marker")
                == "persistent"
            )
        assert (
            await guest(clone, "test ! -f /root/.opencuria-env.sh; echo clean")
            == "clean"
        )
        matrix.passed(15)
        too_small = str(uuid.uuid4())
        try:
            await runtime.create_workspace_from_image_artifact(
                artifact.artifact_id, too_small, qemu_disk_size_gb=5
            )
        except (ValueError, RuntimeError):
            pass
        else:
            raise AssertionError("Too-small disk was accepted")
        await absent(too_small)
        matrix.passed(16)
        try:
            await runtime.delete_image_artifact(artifact.artifact_id)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Backing dependency deletion accepted")
        assert await guest(clone, "cat /workspace/persistence-marker") == "persistent"
        matrix.passed(17)
        marker = capture.with_suffix(".manifest.json")
        original = marker.read_bytes()
        tampered = str(uuid.uuid4())
        try:
            content = json.loads(original)
            content["sha256"] = "0" * 64
            marker.write_text(json.dumps(content))
            try:
                await runtime.create_workspace_from_image_artifact(
                    artifact.artifact_id, tampered, qemu_disk_size_gb=7
                )
            except (RuntimeError, ValueError):
                pass
            else:
                raise AssertionError("Tampered image accepted")
            await absent(tampered)
        finally:
            marker.write_bytes(original)
        matrix.passed(18)
        await service.lifecycle.update_workspace_resources(
            uuid.UUID(second),
            qemu_vcpus=2,
            qemu_memory_mb=1152,
            qemu_disk_size_gb=7,
        )
        domain = await asyncio.to_thread(runtime._get_domain, second)
        xml = ET.fromstring(await asyncio.to_thread(domain.XMLDesc, 0))
        assert int(xml.findtext("vcpu")) == 2
        assert int(xml.findtext("memory")) == 1152 * 1024
        assert await guest(second, "cat /workspace/build-marker") == "build-1"
        matrix.passed(19)
        try:
            await runtime.reconfigure_workspace(
                second,
                qemu_vcpus=2,
                qemu_memory_mb=1152,
                qemu_disk_size_gb=6,
                restart=True,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Disk shrink was accepted")
        assert (await runtime.get_workspace_status(second)).status == "running"
        assert await guest(second, "cat /workspace/build-marker") == "build-1"
        matrix.passed(20)
        try:
            await runtime.reconfigure_workspace(
                second,
                qemu_vcpus=2,
                qemu_memory_mb=1000000,
                qemu_disk_size_gb=7,
                restart=True,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Impossible RAM accepted")
        assert (await runtime.get_workspace_status(second)).status == "running"
        assert await guest(second, "cat /workspace/build-marker") == "build-1"
        matrix.passed(21)
        await settled(
            service.lifecycle.stop_workspace(uuid.UUID(second)),
            service.lifecycle.resume_workspace(
                uuid.UUID(second),
                qemu_vcpus=2,
                qemu_memory_mb=1152,
                qemu_disk_size_gb=7,
            ),
        )
        assert (await runtime.get_workspace_status(second)).status == "running"
        matrix.passed(22)
        domain = await asyncio.to_thread(runtime._get_domain, second)
        await asyncio.to_thread(domain.suspend)
        await runtime.stop_workspace(second)
        assert not await asyncio.to_thread(domain.isActive)
        matrix.passed(23)
        await service.lifecycle.resume_workspace(
            uuid.UUID(second), qemu_vcpus=2, qemu_memory_mb=1152, qemu_disk_size_gb=7
        )
        assert await guest(second, "cat /workspace/build-marker") == "build-1"
        await service.lifecycle.stop_workspace(uuid.UUID(second))
        assert await interface._workspace_scrub_proof(uuid.UUID(second))
        matrix.passed(24)
        before = (await runtime.workspace_incarnation(first))[0]
        await service.lifecycle.remove_workspace(uuid.UUID(first))
        assert await guest(clone, "cat /workspace/persistence-marker") == "persistent"
        matrix.passed(25)
        await service.lifecycle.create_workspace(
            repos=[],
            workspace_id=uuid.UUID(first),
            base_image_path=str(paths[1]),
            qemu_vcpus=1,
            qemu_memory_mb=1024,
            qemu_disk_size_gb=6,
        )
        after = (await runtime.workspace_incarnation(first))[0]
        assert before != after
        assert await guest(first, "cat /workspace/build-marker") == "build-1"
        matrix.passed(26)
        await service.lifecycle.remove_workspace(uuid.UUID(first))
        await runtime.remove_workspace(first)
        await absent(first)
        matrix.passed(27)
        await service.lifecycle.remove_workspace(uuid.UUID(clone))
        await runtime.delete_image_artifact(artifact.artifact_id)
        assert not capture.exists()
        matrix.passed(28)
        failed_op = str(uuid.uuid4())
        failed_builder = runtime._build_instance_id(failed_op)
        owned.add(failed_builder)
        failed_path = runtime._build_image_dir / f"{uuid.uuid4()}.qcow2"
        images.add(failed_path)
        try:
            await runtime.build_image(
                base_distro=f"ubuntu:{distro}",
                init_script="#!/bin/bash\nexit 42\n",
                image_path=str(failed_path),
                operation_id=failed_op,
                image_instance_id=str(uuid.uuid4()),
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Failed init incorrectly succeeded")
        assert not failed_path.exists()
        await absent(failed_builder)
        matrix.passed(29)
        after_path = runtime._build_image_dir / f"{uuid.uuid4()}.qcow2"
        images.add(after_path)
        await runtime.build_image(
            base_distro=f"ubuntu:{distro}",
            init_script="#!/bin/bash\nset -e\necho after-failure >/workspace/build-marker\n",
            image_path=str(after_path),
            operation_id=str(uuid.uuid4()),
            image_instance_id=str(uuid.uuid4()),
        )
        assert after_path.with_suffix(".manifest.json").exists()
        matrix.passed(30)
        crash_id = str(uuid.uuid4())
        owned.add(crash_id)
        crash_command = command(crash_id, str(paths[0]))
        (root / "crash-command.json").write_text(json.dumps(crash_command))
        original_script = Path(__file__).with_name("verify_workspace_lifecycle.py")
        with (root / "crash-child.log").open("w") as log:
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                str(original_script),
                "--crash-child",
                "--output",
                str(root),
                stdout=log,
                stderr=log,
            )

            async def wait_child() -> None:
                while not (root / "child-ready").exists():
                    if child.returncode is not None:
                        raise RuntimeError(
                            "Crash child failed; inspect crash-child.log"
                        )
                    await asyncio.sleep(0.1)

            await bounded(wait_child())
            child.send_signal(signal.SIGKILL)
            await child.wait()
        restart_runtime, restart_settings = runtime_at(root, "crash-state")
        restart_service = WorkspaceService(restart_runtime, restart_settings)
        restarted = WebSocketInterface(restart_service, restart_settings)
        restarted._operations.emit = emit
        # The matrix deliberately injects transport instead of a deployed backend.
        # Reflect its restored connection in the Socket.IO test fixture as well.
        restarted._sio.connected = online
        await restarted._operations.replay()
        pending = await restarted._operations.journal.call(
            "pending", crash_command["operation_id"]
        )
        assert pending and pending[0][0] == "workspace:error", pending
        before = (await restart_runtime.workspace_incarnation(crash_id))[0]
        await restarted._sio.handlers["/"]["task:create_workspace"](crash_command)
        after = (await restart_runtime.workspace_incarnation(crash_id))[0]
        assert before == after
        inspection = await restarted._operations.inspect(crash_command)
        assert inspection["execution_finished"] and inspection["quiescent"], inspection
        # Recovery stops an unproven initialization to terminate remote effects.
        # Only this explicit test action starts it again to inspect preserved data.
        await restart_runtime.start_workspace(crash_id)
        assert await guest(crash_id, "cat /workspace/build-marker") == "build-0"
        matrix.passed(31, evidence=inspection)
        await restart_runtime.remove_workspace(crash_id)
        await absent(crash_id)
        matrix.passed(32)
        matrix.report["passed"] = True
    except BaseException as error:
        matrix.report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        # Drain every issued operation before cleaning resources it might own.
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
        cleanup_errors = []
        for instance_id in owned:
            try:
                await runtime.remove_workspace(instance_id)
            except Exception as error:  # noqa: BLE001 - clean every owned resource
                cleanup_errors.append(f"{instance_id}: {error}")
        if not cleanup_errors:
            for image in images:
                try:
                    if image.exists():
                        await runtime.delete_image_artifact(str(image))
                except Exception as error:  # noqa: BLE001 - preserve cleanup evidence
                    cleanup_errors.append(f"{image}: {error}")
        if restarted is not None:
            restarted._operations.journal.close()
        interface._operations.journal.close()
        matrix.report["cleanup_errors"] = cleanup_errors
        matrix.report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if cleanup_errors:
            matrix.report["passed"] = False
        matrix.write()
        if cleanup_errors:
            raise RuntimeError(f"Test resource cleanup failed: {cleanup_errors}")


def main() -> None:
    """Require explicit local fixtures and a fresh output root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cloud-image", required=True, type=Path)
    parser.add_argument("--distro", choices=("22.04", "24.04"), required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.output.resolve(), args.cloud_image.resolve(), args.distro))


if __name__ == "__main__":
    main()
