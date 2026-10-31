"""Real KVM process crashes at initialization/publication boundaries.

V066, V069 and V073 are declared in docs/testing/lifecycle-vm-cases.md.
Only resources belonging to a fresh output directory are changed. The test
uses a real durable journal and the production Socket.IO lifecycle handlers;
transport emission is captured locally, without contacting a deployed backend.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

from src.interfaces.websocket import WebSocketInterface
from src.service import WorkspaceService
from verify_workspace_lifecycle import command, runtime_at


def build_command(root: Path, mode: str) -> dict[str, Any]:
    """Create one identity whose recipe must never be replayed after a crash."""
    operation, image = str(uuid.uuid4()), str(uuid.uuid4())
    recipe = (
        "#!/bin/bash\nset -euo pipefail\nmkdir -p /workspace\n"
        "echo attempt >>/workspace/build-attempts\n"
        "echo AUDIT-INIT-STARTED\n"
    )
    if mode == "initializing":
        recipe += "sleep 600\n"
    recipe += "echo completed >/workspace/crash-marker\nsync\n"
    return {
        "task_id": operation,
        "operation_id": operation,
        "attempt": 1,
        "runner_id": str(uuid.uuid4()),
        "target": image,
        "build_job_id": str(uuid.uuid4()),
        "image_instance_id": image,
        "image_path": str(root / "builds" / f"{image}.qcow2"),
        "base_distro": "ubuntu:24.04",
        "init_script": recipe,
    }


async def child(root: Path, cloud: Path, mode: str) -> None:
    """Block only after the named physical side effect has actually occurred."""
    runtime, settings = runtime_at(root)
    runtime._resolve_build_base_image = lambda _: cloud
    service = WorkspaceService(runtime, settings)
    interface = WebSocketInterface(service, settings)

    async def barrier(label: str) -> None:
        (root / "child-ready").write_text(label)
        await asyncio.Event().wait()

    async def emitted(event: str, data: Any = None, **kwargs: Any) -> None:
        if (
            mode == "initializing"
            and event == "image:build_progress"
            and "AUDIT-INIT-STARTED" in (data or {}).get("line", "")
        ):
            await barrier("real guest initialization started")

    interface._operations.emit = emitted
    if mode == "published":
        finalize = runtime._finalize_build_image

        async def finalized(*args: Any, **kwargs: Any) -> dict[str, Any]:
            result = await finalize(*args, **kwargs)
            await barrier("physical publication committed; terminal not journaled")
            return result

        runtime._finalize_build_image = finalized
    elif mode == "partial":
        network = runtime._create_workspace_network

        async def before_network(instance_id: str) -> Any:
            assert runtime._disk_path(instance_id).exists()
            await barrier("physical disk allocated; no domain or network defined")
            return await network(instance_id)

        runtime._create_workspace_network = before_network

    data = json.loads((root / "command.json").read_text())
    event = "task:create_workspace" if mode == "partial" else "task:build_image"
    await interface._sio.handlers["/"][event](data)


async def verify(root: Path, cloud: Path) -> None:
    """Kill a real runner child, restart, and verify exact automatic recovery."""
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o755)
    if any(not parent.stat().st_mode & 0o001 for parent in root.parents):
        raise PermissionError("Choose an output parent traversable by libvirt-qemu")
    cached = root / "cloud.img"
    await asyncio.to_thread(shutil.copyfile, cloud, cached)
    cached.chmod(0o644)
    report: dict[str, Any] = {"cases": {}, "passed": False, "cleanup_errors": []}

    def write() -> None:
        (root / "report.json").write_text(json.dumps(report, indent=2))

    for mode, case in (
        ("initializing", "V066"),
        ("published", "V069"),
        ("partial", "V073"),
    ):
        scenario = root / mode
        scenario.mkdir(mode=0o755)
        runtime, settings = runtime_at(scenario)
        runtime._resolve_build_base_image = lambda _: cached
        data = (
            command(str(uuid.uuid4()), str(cached))
            if mode == "partial"
            else build_command(scenario, mode)
        )
        target = (
            data["workspace_id"]
            if mode == "partial"
            else runtime._build_instance_id(data["operation_id"])
        )
        (scenario / "command.json").write_text(json.dumps(data))
        process = None
        interface = None
        inspection_guest = None
        try:
            with (scenario / "child.log").open("w") as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--output",
                    str(scenario),
                    "--cloud-image",
                    str(cached),
                    "--child-mode",
                    mode,
                    stdout=log,
                    stderr=log,
                )

                async def ready(
                    scenario_path: Path = scenario,
                    child_process: asyncio.subprocess.Process = process,
                    scenario_mode: str = mode,
                ) -> None:
                    while not (scenario_path / "child-ready").exists():
                        if child_process.returncode is not None:
                            raise RuntimeError(
                                f"{scenario_mode} child exited: inspect child.log"
                            )
                        await asyncio.sleep(0.1)

                await asyncio.wait_for(ready(), 360)
                if mode == "partial":
                    assert not await runtime.workspace_exists(target)
                    assert runtime._disk_path(target).exists()
                else:
                    assert await runtime.workspace_exists(target)
                if mode == "published":
                    publication = Path(data["image_path"])
                    assert publication.with_suffix(".manifest.json").exists()
                    inode = publication.stat().st_ino
                process.send_signal(signal.SIGKILL)
                await process.wait()

            service = WorkspaceService(runtime, settings)
            interface = WebSocketInterface(service, settings)
            interface._sio.connected = True
            emissions = []
            recipe_attempts = [0]

            async def emitted(
                event: str,
                payload: Any = None,
                *,
                captured: list[tuple[str, Any]] = emissions,
                **kwargs: Any,
            ) -> None:
                captured.append((event, payload))

            async def forbidden_recipe(
                *args: Any,
                attempts: list[int] = recipe_attempts,
                **kwargs: Any,
            ) -> None:
                attempts[0] += 1
                raise AssertionError("Recovery attempted to replay initialization")

            interface._operations.emit = emitted
            runtime._stream_ssh_process = forbidden_recipe

            async def recovered(
                operations: Any = interface._operations,
                operation_id: str = data["operation_id"],
            ) -> list[Any]:
                # The production replay loop retries while surviving disk
                # helpers can still have effects; one tick need not settle it.
                while True:
                    await operations.replay()
                    records = await operations.journal.call("pending", operation_id)
                    if records:
                        return records
                    await asyncio.sleep(0.1)

            pending = await asyncio.wait_for(recovered(), 240)
            assert len(pending) == 1, pending
            expected = (
                "image:built"
                if mode == "published"
                else ("workspace:error" if mode == "partial" else "image:build_failed")
            )
            assert pending[0][0] == expected, pending
            assert pending[0][1]["execution_finished"] is True
            assert pending[0][1]["outcome_known"] is (mode == "published")
            original_result = await interface._operations.journal.call(
                "lookup", data["operation_id"]
            )
            event = "task:create_workspace" if mode == "partial" else "task:build_image"
            await interface._sio.handlers["/"][event](data)
            assert recipe_attempts == [0], recipe_attempts
            assert (
                await interface._operations.journal.call("lookup", data["operation_id"])
                == original_result
            )

            async def quiescent(
                operations: Any = interface._operations,
                identity: dict[str, Any] = data,
            ) -> dict[str, Any]:
                while True:
                    evidence = await operations.inspect(identity)
                    if evidence.get("execution_finished") and evidence.get("quiescent"):
                        return evidence
                    await asyncio.sleep(0.1)

            evidence = await asyncio.wait_for(quiescent(), 240)
            assert evidence["execution_finished"] and evidence["quiescent"], evidence

            if mode == "published":
                assert publication.stat().st_ino == inode
                inspection_guest = str(uuid.uuid4())
                await service.lifecycle.create_workspace(
                    repos=[],
                    workspace_id=uuid.UUID(inspection_guest),
                    base_image_path=str(publication),
                    qemu_vcpus=1,
                    qemu_memory_mb=1024,
                    qemu_disk_size_gb=6,
                )
                code, output = await runtime.exec_command_wait(
                    inspection_guest,
                    [
                        "bash",
                        "-lc",
                        "cat /workspace/build-attempts /workspace/crash-marker",
                    ],
                )
                assert code == 0 and output.strip().splitlines() == [
                    "attempt",
                    "completed",
                ], output
                await service.lifecycle.remove_workspace(uuid.UUID(inspection_guest))
                inspection_guest = None
            elif mode == "initializing":
                assert await runtime.workspace_removed(target)
                assert not Path(data["image_path"]).exists()
            else:
                # Files remain after quiescence, but an ordinary remove command
                # uses persisted allocation ownership to clean them after restart.
                assert runtime._disk_path(target).exists()
                remove = {
                    **command(target, str(cached)),
                    "operation_id": str(uuid.uuid4()),
                }
                remove["task_id"] = remove["operation_id"]
                await interface._sio.handlers["/"]["task:remove_workspace"](remove)
                removal = await interface._operations.journal.call(
                    "pending", remove["operation_id"]
                )
                assert removal and removal[0][0] == "workspace:removed", removal
                assert await runtime.workspace_removed(target)
                await interface._operations.acknowledge(remove)
            await interface._operations.acknowledge(data)
            assert not await interface._operations.journal.call("pending")
            await interface._sio.handlers["/"][event](data)
            assert recipe_attempts == [0], recipe_attempts
            assert (
                await interface._operations.journal.call("lookup", data["operation_id"])
                == original_result
            )
            assert not await interface._operations.journal.call("pending")
            report["cases"][case] = {
                "status": "PASS",
                "process_exit": process.returncode,
                "mode": mode,
                "terminal": expected,
                "recipe_replayed": bool(recipe_attempts[0]),
                "evidence": evidence,
                "matching_ack_clears_result": True,
            }
            write()
        except BaseException as error:
            report["error"] = f"{case}: {type(error).__name__}: {error}"
            raise
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            for instance in (target, inspection_guest):
                if instance is None:
                    continue
                try:
                    await runtime.remove_workspace(instance)
                except Exception as error:  # noqa: BLE001 - clean every owned resource
                    report["cleanup_errors"].append(f"{instance}: {error}")
            if mode != "partial" and Path(data["image_path"]).exists():
                try:
                    await runtime.delete_image_artifact(data["image_path"])
                except Exception as error:  # noqa: BLE001 - preserve cleanup evidence
                    report["cleanup_errors"].append(f"publication: {error}")
            if interface is not None:
                interface._operations.journal.close()
            write()
    report["passed"] = not report["cleanup_errors"]
    write()
    if not report["passed"]:
        raise RuntimeError(f"Test cleanup failed: {report['cleanup_errors']}")


def main() -> None:
    """Require explicit fixtures; parent runs reject output-directory reuse."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cloud-image", required=True, type=Path)
    parser.add_argument(
        "--child-mode", choices=("initializing", "published", "partial")
    )
    args = parser.parse_args()
    function = (
        child(args.output.resolve(), args.cloud_image.resolve(), args.child_mode)
        if args.child_mode
        else verify(args.output.resolve(), args.cloud_image.resolve())
    )
    asyncio.run(function)


if __name__ == "__main__":
    main()
