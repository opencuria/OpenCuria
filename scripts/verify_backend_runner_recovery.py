"""Opt-in recovery checks using real backend, Redis, runner and KVM guests.

Run with the runner virtualenv and an installed backend virtualenv. Every service
uses a fresh local database, a separate loopback Redis instance and fresh guest
identities. No deployed backend is contacted. Faults are injected immediately
before capture publication, with actual backend and runner process SIGKILLs.
Only guests allocated by this invocation are removed. Private test credentials,
SSH keys and journals remain below --output; share report.json only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "runner"))
sys.path.insert(0, str(PROJECT / "scripts"))


def seed(root: Path) -> None:
    """Seed one fixture image, runner and account in the isolated database."""
    sys.path.insert(0, str(PROJECT / "backend"))
    import django

    django.setup()
    from apps.organizations.models import Membership, Organization
    from apps.runners.models import (
        ImageBuildJob,
        ImageDefinition,
        ImageInstance,
        ImageRevision,
        Runner,
    )
    from common.utils import hash_token
    from django.contrib.auth import get_user_model

    config = json.loads((root / "config.json").read_text())
    user = get_user_model().objects.create_user(
        email="audit@localhost.test",
        username="audit",
        password=config["password"],
        is_staff=True,
        is_superuser=True,
    )
    org = Organization.objects.create(
        name="Isolated lifecycle audit", slug="lifecycle-audit"
    )
    Membership.objects.create(user=user, organization=org, role="admin")
    runner = Runner.objects.create(
        name="isolated-lifecycle-audit",
        api_token_hash=hash_token(config["runner_token"]),
        organization=org,
        qemu_default_vcpus=1,
        qemu_default_memory_mb=1024,
        qemu_min_disk_size_gb=6,
        qemu_default_disk_size_gb=6,
    )
    definition = ImageDefinition.objects.create(
        name="Isolated cloud fixture",
        organization=org,
        created_by=user,
        base_distro="ubuntu:24.04",
    )
    revision = ImageRevision.objects.create(
        definition=definition, digest="a" * 64, recipe={"base_distro": "ubuntu:24.04"}
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition, runner=runner, status="active"
    )
    image = ImageInstance.objects.create(
        runner=runner,
        name=definition.name,
        origin_type="definition_build",
        origin_definition=definition,
        build_job=build,
        revision=revision,
        generation=1,
        runner_ref=str(root / "cache/cloud.img"),
        status="ready",
        min_disk_size_gb=6,
    )
    build.current_generation = image
    build.save(update_fields=["current_generation"])
    config.update(org_id=str(org.id), runner_id=str(runner.id), image_id=str(image.id))
    (root / "config.json").write_text(json.dumps(config))
    (root / "config.json").chmod(0o600)


async def runner(root: Path) -> None:
    """Run the production service with a labelled capture fault boundary."""
    from src.interfaces.websocket import WebSocketInterface
    from src.service import WorkspaceService
    from verify_workspace_lifecycle import runtime_at

    config = json.loads((root / "config.json").read_text())
    runtime, settings = runtime_at(root)
    settings.backend_url = config["url"]
    settings.api_token = config["runner_token"]
    settings.heartbeat_interval = 1
    settings.inventory_interval = 15
    service = WorkspaceService(runtime, settings)
    await service.registry.sync_from_runtime()
    interface = WebSocketInterface(service, settings)
    interface._sio.reconnection_delay = 0.2
    interface._sio.reconnection_delay_max = 1
    interface._sio.randomization_factor = 0
    capture = runtime.create_image_artifact

    async def injected(*args: Any, **kwargs: Any) -> Any:
        marker = root / "inject-capture"
        if marker.exists():
            mode = marker.read_text()
            marker.unlink()
            (root / "capture-entered").write_text(mode)
            while not (root / "capture-release").exists():
                await asyncio.sleep(0.05)
            (root / "capture-release").unlink()
            if mode == "fail":
                raise RuntimeError("injected capture write failure before publication")
        return await capture(*args, **kwargs)

    runtime.create_image_artifact = injected
    try:
        await interface.start()
    finally:
        await interface.stop()
        interface._operations.journal.close()


def free_port() -> int:
    """Choose an unoccupied loopback port for an isolated local service."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def verify(root: Path, cloud: Path) -> None:
    """Create real guests and verify automatic capture recovery across crashes."""
    import aiohttp
    from verify_workspace_lifecycle import runtime_at

    root.mkdir(mode=0o755, parents=True, exist_ok=False)
    root.chmod(0o755)
    if any(not parent.stat().st_mode & 0o001 for parent in root.parents):
        raise PermissionError("Choose an output parent traversable by libvirt-qemu")
    (root / "cache").mkdir(mode=0o755)
    await asyncio.to_thread(shutil.copyfile, cloud, root / "cache/cloud.img")
    (root / "cache/cloud.img").chmod(0o644)
    config = {
        "url": f"http://127.0.0.1:{free_port()}",
        "password": str(uuid.uuid4()),
        "runner_token": str(uuid.uuid4()),
    }
    (root / "config.json").write_text(json.dumps(config))
    (root / "config.json").chmod(0o600)
    redis_port = free_port()
    env = {
        **os.environ,
        "DJANGO_ENV": "development",
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "SQLITE_PATH": str(root / "backend.sqlite3"),
        "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
    }
    processes: dict[str, asyncio.subprocess.Process] = {}
    logs: list[TextIO] = []
    report: dict[str, Any] = {"checks": [], "passed": False, "cleanup_errors": []}
    owned: set[str] = set()
    runtime, _ = runtime_at(root)
    script = str(Path(__file__).resolve())
    backend_python = str(PROJECT / "backend/.venv/bin/python")

    async def spawn(
        name: str, *cmd: str, cwd: Path = PROJECT / "backend"
    ) -> asyncio.subprocess.Process:
        logfile = (root / f"{name}.log").open("a")
        logs.append(logfile)
        process = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env, stdout=logfile, stderr=logfile
        )
        processes[name] = process
        return process

    async def stop(name: str, kill: bool = False) -> None:
        proc = processes.pop(name, None)
        if proc and proc.returncode is None:
            proc.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    async def backend() -> None:
        # Match compose restart supervision while a crashed SQLite owner's
        # 45-second scheduler lease expires. Never rewrite ownership in tests.
        async with asyncio.timeout(90):
            async with aiohttp.ClientSession() as client:
                while True:
                    proc = await spawn(
                        "backend",
                        backend_python,
                        "-m",
                        "uvicorn",
                        "config.asgi:application",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        config["url"].rsplit(":", 1)[1],
                        "--lifespan",
                        "on",
                    )
                    while proc.returncode is None:
                        try:
                            async with client.get(
                                config["url"] + "/api/v1/health/"
                            ) as response:
                                if response.status == 200:
                                    return
                        except (aiohttp.ClientError, asyncio.TimeoutError):
                            pass
                        await asyncio.sleep(0.2)
                    await asyncio.sleep(1)

    def record(name: str, **evidence: Any) -> None:
        report["checks"].append({"name": name, **evidence})
        (root / "report.json").write_text(json.dumps(report, indent=2))

    async def wait_file(name: str, timeout: float = 180) -> None:
        async with asyncio.timeout(timeout):
            while not (root / name).exists():
                proc = processes.get("runner")
                if proc and proc.returncode is not None:
                    raise RuntimeError(
                        f"runner exited {proc.returncode}; inspect runner.log"
                    )
                await asyncio.sleep(0.2)

    try:
        await spawn(
            "redis",
            "redis-server",
            "--port",
            str(redis_port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(root),
            cwd=root,
        )
        proc = await spawn(
            "migrate", backend_python, "manage.py", "migrate", "--noinput"
        )
        assert await asyncio.wait_for(proc.wait(), 120) == 0, "migrate failed"
        proc = await spawn(
            "seed", backend_python, script, "--output", str(root), "--seed"
        )
        assert await asyncio.wait_for(proc.wait(), 30) == 0, "seed failed"
        config = json.loads((root / "config.json").read_text())
        await backend()
        await spawn("recovery", backend_python, "manage.py", "recover_lifecycle")
        await spawn(
            "runner",
            sys.executable,
            script,
            "--output",
            str(root),
            "--runner",
            cwd=PROJECT,
        )
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25)
        ) as session:
            headers = {"X-Organization-Id": config["org_id"]}

            async def api(
                method: str,
                path: str,
                payload: dict[str, Any] | None = None,
                expected: int = 200,
            ) -> Any:
                async with session.request(
                    method,
                    config["url"] + "/api/v1/" + path,
                    json=payload,
                    headers=headers,
                ) as response:
                    data = await response.json()
                    if response.status != expected:
                        raise AssertionError((method, path, response.status, data))
                    return data

            async with asyncio.timeout(30):
                while True:
                    try:
                        await api("GET", "health/")
                        break
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        await asyncio.sleep(0.2)
            token = await api(
                "POST",
                "auth/login/",
                {"email": "audit@localhost.test", "password": config["password"]},
            )
            headers["Authorization"] = "Bearer " + token["access_token"]

            async def online() -> None:
                async with asyncio.timeout(30):
                    while True:
                        try:
                            runners = await api("GET", "runners/")
                        except (aiohttp.ClientError, asyncio.TimeoutError):
                            await asyncio.sleep(0.3)
                            continue
                        if any(
                            row["id"] == config["runner_id"]
                            and row["status"] == "online"
                            for row in runners
                        ):
                            return
                        await asyncio.sleep(0.3)

            async def settled(
                ws_id: str, expected_status: str | None = None, timeout: float = 240
            ) -> dict[str, Any]:
                async with asyncio.timeout(timeout):
                    while True:
                        row = await api("GET", f"workspaces/{ws_id}/")
                        if (
                            not row["active_operation"]
                            and not row["intervention_required"]
                            and (
                                expected_status is None
                                or row["status"] == expected_status
                            )
                        ):
                            assert not row["intervention_required"], row
                            return row
                        await asyncio.sleep(0.3)

            async def guest(ws_id: str, command: str) -> str:
                # The actual runner may have recreated this guest between
                # observations. Read its newly persisted host-key proof rather
                # than retaining an observer's old in-memory SSH incarnation.
                observer, _ = runtime_at(root)
                try:
                    code, output = await observer.exec_command_wait(
                        ws_id, ["bash", "-lc", command]
                    )
                    assert code == 0, (code, output)
                    return output.strip()
                finally:
                    await observer._invalidate_ssh(ws_id)

            await online()
            row = await api(
                "POST",
                "workspaces/",
                {
                    "name": "isolated audit guest",
                    "image_artifact_id": config["image_id"],
                    "qemu_vcpus": 1,
                    "qemu_memory_mb": 1024,
                    "qemu_disk_size_gb": 6,
                },
                expected=202,
            )
            ws_id = row["workspace_id"]
            owned.add(ws_id)
            await settled(ws_id, "running")
            await guest(ws_id, "echo persistent-audit-marker > /workspace/audit-marker")
            record(
                "real HTTP create, Socket.IO execution, SSH and persisted data",
                workspace_id=ws_id,
            )

            (root / "inject-capture").write_text("fail")
            await api(
                "POST",
                f"workspaces/{ws_id}/image-artifacts/",
                {"name": "failed-capture"},
                expected=202,
            )
            await wait_file("capture-entered")
            await stop("backend", kill=True)
            # The backend is really absent before runner execution is released.
            await asyncio.sleep(1)
            (root / "capture-release").write_text("release")
            await asyncio.sleep(1)
            await backend()
            await online()
            row = await settled(ws_id, "running")
            assert (
                await guest(ws_id, "cat /workspace/audit-marker")
                == "persistent-audit-marker"
            )
            record(
                "backend SIGKILL during failed capture; durable replay and automatic resume",
                final_status=row["status"],
                intervention_required=row["intervention_required"],
            )

            (root / "capture-entered").unlink(missing_ok=True)
            (root / "inject-capture").write_text("crash")
            await api(
                "POST",
                f"workspaces/{ws_id}/image-artifacts/",
                {"name": "interrupted-capture"},
                expected=202,
            )
            await wait_file("capture-entered")
            await stop("runner", kill=True)
            await spawn(
                "runner",
                sys.executable,
                script,
                "--output",
                str(root),
                "--runner",
                cwd=PROJECT,
            )
            await online()
            row = await settled(ws_id, timeout=90)
            assert row["status"] in {"running", "stopped"}, row
            if row["status"] == "stopped":
                await api("POST", f"workspaces/{ws_id}/resume/", expected=202)
                await settled(ws_id, "running")
            assert (
                await guest(ws_id, "cat /workspace/audit-marker")
                == "persistent-audit-marker"
            )
            record(
                "runner SIGKILL during capture; automatic reservation release, usable disk",
                recovered_status=row["status"],
                intervention_required=row["intervention_required"],
            )

            await api(
                "POST",
                f"workspaces/{ws_id}/image-artifacts/",
                {"name": "successful-retry"},
                expected=202,
            )
            row = await settled(ws_id, "running", timeout=360)
            assert (
                await guest(ws_id, "cat /workspace/audit-marker")
                == "persistent-audit-marker"
            )
            captures = await api("GET", "captured-images/")
            ready = [
                version
                for line in captures
                for version in line["versions"]
                if version["status"] == "ready"
            ]
            assert ready, captures
            record(
                "capture after failures succeeds; automatic replacement preserves data",
                base_image=row["base_image"],
                ready_versions=len(ready),
            )
            await api("DELETE", f"workspaces/{ws_id}/", expected=202)
            await settled(ws_id, "deleted")
            assert await runtime.workspace_removed(ws_id)
            owned.remove(ws_id)
            record(
                "HTTP deletion reaches deleted; physical domain, network and disk absent"
            )
            report["passed"] = True
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for name in list(processes):
            await stop(name)
        for ws_id in owned:
            try:
                await runtime.remove_workspace(ws_id)
            except Exception as exc:  # noqa: BLE001 - clean every owned resource
                report["cleanup_errors"].append(f"{ws_id}: {exc}")
        if report["cleanup_errors"]:
            report["passed"] = False
        for log in logs:
            log.close()
        (root / "report.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cloud-image", type=Path)
    parser.add_argument("--seed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--runner", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.seed:
        seed(args.output)
    elif args.runner:
        asyncio.run(runner(args.output))
    else:
        if args.cloud_image is None:
            parser.error("--cloud-image is required")
        asyncio.run(
            verify(args.output.resolve(), args.cloud_image.resolve(strict=True))
        )
