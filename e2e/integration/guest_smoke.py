"""Optional booted Ubuntu runtime/service test. Parent runs; no HTTP coverage.

Requires parent-prepared storage ACLs for libvirt. Never changes host permissions,
existing guests, or the pristine source. Failure leaves exact UUID evidence for
explicit cleanup. Uses a distinct SQLite journal, never the live daemon journal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import uuid
from pathlib import Path

from support import command, load, save

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runner"))
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.runtime.qemu_runtime import QemuRuntime
from src.service import WorkspaceService
from src.services.credentials import (
    WORKSPACE_CREDENTIAL_ENV_FILE,
    WORKSPACE_CREDENTIAL_PROFILE_D,
    WORKSPACE_CREDENTIAL_ENVIRONMENT,
)

SOURCE = Path("/var/lib/opencuria/images/ubuntu-24.04-server-cloudimg-amd64.img")


def settings() -> RunnerSettings:
    """Use dedicated explicitly selected parent paths, not production .env."""
    values = {}
    for key in (
        "qemu_image_cache_dir",
        "qemu_disk_dir",
        "qemu_snapshot_dir",
        "qemu_ssh_key_path",
    ):
        path = Path(os.environ["RUNNER_" + key.upper()]).resolve()
        if not path.is_relative_to("/var/lib/libvirt/images/opencuria-integration"):
            raise ValueError(
                "Guest storage must be inside dedicated libvirt integration directory"
            )
        values[key] = str(path)
    values["state_dir"] = str(Path(os.environ["INTEGRATION_RUN_DIR"]) / "guest-journal")
    return RunnerSettings(_env_file=None, **values)


def prepare() -> None:
    """Copy only the known public pristine source into a new parent-owned cache."""
    cfg = settings()
    base = Path(cfg.qemu_image_cache_dir) / "full-guest-base.qcow2"
    if (
        base.exists()
        or (Path(os.environ["INTEGRATION_RUN_DIR"]) / "guest.json").exists()
    ):
        raise RuntimeError("Refusing to overwrite an existing guest fixture")
    if not SOURCE.is_file():
        raise RuntimeError("Known pristine public cloud image is missing")
    info = json.loads(command("qemu-img", "info", "--output=json", str(SOURCE)))
    assert not info.get("backing-filename") and info["format"] == "qcow2"
    wid, image = str(uuid.uuid4()), str(uuid.uuid4())
    save(
        "guest.json",
        {
            "workspace_id": wid,
            "image_id": image,
            "base": str(base),
            "disk_dir": cfg.qemu_disk_dir,
            "snapshot_dir": cfg.qemu_snapshot_dir,
        },
    )
    # qemu-img convert reads source only; never uses resize or writes its inode.
    command("qemu-img", "convert", "-O", "qcow2", str(SOURCE), str(base))
    command("qemu-img", "check", str(base))


async def assert_credential_files(
    rt: QemuRuntime, wid: uuid.UUID, *, present: bool
) -> None:
    """Check real managed credential paths without printing credential contents."""
    env_file = shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)
    profile = shlex.quote(WORKSPACE_CREDENTIAL_PROFILE_D)
    environment = shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)
    if present:
        script = (
            f"test -f {env_file} && test -f {profile} && "
            f'. {env_file} && test -n "${{OPENCURIA_INTEGRATION_SECRET:-}}"'
        )
    else:
        script = (
            f"test ! -e {env_file} && test ! -e {profile} && "
            f"test -f {environment} && ! grep -q '^OPENCURIA_INTEGRATION_SECRET=' {environment} && "
            'test -z "${OPENCURIA_INTEGRATION_SECRET:-}"'
        )
    code, _ = await rt.exec_command_wait(str(wid), ["bash", "-lc", script])
    assert code == 0, "Managed credential presence/absence assertion failed"


async def assert_shutoff(wid: uuid.UUID) -> None:
    """Do not confuse libvirt SHUTDOWN/paused with completed disk quiescence."""
    state = await asyncio.to_thread(
        command,
        "virsh",
        "-c",
        "qemu:///system",
        "domstate",
        f"opencuria-workspace-{wid}",
    )
    assert state.strip() == "shut off", "Guest stop did not complete"


async def smoke() -> None:
    """Normal create/inject/scrub/capture/resume with actual checkpoint wiring."""
    state = load("guest.json")
    cfg = settings()
    assert cfg.qemu_disk_dir == state["disk_dir"]
    rt = QemuRuntime(cfg)
    service = WorkspaceService({"qemu": rt}, cfg)
    interface = WebSocketInterface(
        service, cfg
    )  # real journal/checkpoint hooks; no socket connect
    wid = uuid.UUID(state["workspace_id"])
    # Controlled credential must be parent-supplied. Never store/log its value.
    credential = os.environ["INTEGRATION_GUEST_TEST_CREDENTIAL"]
    assert credential
    created, present = await service.create_workspace(
        [],
        workspace_id=wid,
        runtime_type="qemu",
        base_image_path=state["base"],
        qemu_vcpus=1,
        qemu_memory_mb=1024,
        qemu_disk_size_gb=20,
        env_vars={"OPENCURIA_INTEGRATION_SECRET": credential},
    )
    assert created == wid and present
    await assert_credential_files(rt, wid, present=True)
    assert (await rt.get_workspace_status(str(wid))).status == "running"
    await service.stop_workspace(wid)
    await assert_shutoff(wid)
    assert await interface._workspace_scrub_proof(wid)
    assert not await service.resume_workspace(
        wid, qemu_vcpus=1, qemu_memory_mb=1024, qemu_disk_size_gb=20
    )
    await assert_credential_files(rt, wid, present=False)
    await service.stop_workspace(wid)
    await assert_shutoff(wid)
    assert await interface._workspace_scrub_proof(wid)
    await service.images.create_image_artifact(
        wid,
        "Booted pristine Ubuntu integration capture",
        artifact_id=state["image_id"],
        operation_id=str(uuid.uuid4()),
    )
    info = json.loads(
        command(
            "qemu-img",
            "info",
            "--output=json",
            str(rt._managed_image_path(state["image_id"])),
        )
    )
    assert not info.get("backing-filename")
    command("qemu-img", "check", str(rt._managed_image_path(state["image_id"])))
    assert await service.resume_workspace(
        wid,
        qemu_vcpus=1,
        qemu_memory_mb=1024,
        qemu_disk_size_gb=20,
        env_vars={"OPENCURIA_INTEGRATION_SECRET": credential},
    )
    await assert_credential_files(rt, wid, present=True)
    assert (await rt.get_workspace_status(str(wid))).status == "running"
    # ACPI stop outside service: inspection must observe it, never auto-start.
    command("virsh", "-c", "qemu:///system", "shutdown", f"opencuria-workspace-{wid}")
    for _ in range(90):
        if (
            command(
                "virsh",
                "-c",
                "qemu:///system",
                "domstate",
                f"opencuria-workspace-{wid}",
            ).strip()
            == "shut off"
        ):
            break
        await asyncio.sleep(2)
    else:
        raise TimeoutError("External guest shutdown did not complete")
    assert not await interface._workspace_scrub_proof(
        wid
    )  # injected credentials, not scrubbed
    await asyncio.sleep(3)
    assert (await rt.get_workspace_status(str(wid))).status in {"exited", "stopped"}
    save(
        "guest-result.json",
        {
            "workspace_id": str(wid),
            "artifact": str(rt._managed_image_path(state["image_id"])),
            "self_contained": True,
            "scrub_checkpoint_verified": True,
            "external_stop_observed": True,
            "coverage": "direct service, not HTTP",
        },
    )
    interface._operations.journal.db.close()


async def cleanup() -> None:
    """Use exact recorded UUIDs and runtime path checks, never enumerate for deletion."""
    state = load("guest.json")
    cfg = settings()
    assert (
        cfg.qemu_disk_dir == state["disk_dir"]
        and cfg.qemu_snapshot_dir == state["snapshot_dir"]
    )
    rt = QemuRuntime(cfg)
    await rt.remove_workspace(state["workspace_id"])
    target = rt._managed_image_path(state["image_id"])
    if target.exists():
        await rt.delete_image_artifact(state["image_id"])
    base = Path(state["base"])
    assert base == Path(cfg.qemu_image_cache_dir) / "full-guest-base.qcow2"
    # Runtime scans must establish no remaining base consumers before unlink.
    scan = await rt.inventory()
    assert scan.complete, scan.errors
    assert not any(str(base) in resource.dependencies for resource in scan.resources)
    base.unlink(missing_ok=True)
    save("guest-cleanup.json", {"workspace_id": state["workspace_id"], "cleaned": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare-guest", "guest-smoke", "cleanup"])
    action = parser.parse_args().action
    if action == "prepare-guest":
        prepare()
    else:
        asyncio.run(smoke() if action == "guest-smoke" else cleanup())
