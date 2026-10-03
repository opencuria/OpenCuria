"""Real shut-off empty QCOW2 capture/dependency/deletion test, no guest OS.

Use runner Python, not backend Python. Never starts a guest or touches existing
libvirt domains. This is NOT full HTTP stop/scrub/resume provisioning coverage:
credential_clean is justified only by a newly created empty disk, never an
existing user's disk. Artifacts can be kept for live inventory/browser inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

import structlog
from support import command, load, run_dir, save

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runner"))
from src.config import RunnerSettings
from src.runtime.qemu_runtime import QemuRuntime

log = structlog.get_logger()


def runtime() -> QemuRuntime:
    """Construct standard runtime with dedicated paths, ignoring runner .env."""
    root = run_dir() / "qemu"
    for child in ("images", "disks", "snapshots"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return QemuRuntime(
        RunnerSettings(
            _env_file=None,
            qemu_image_cache_dir=str(root / "images"),
            qemu_disk_dir=str(root / "disks"),
            qemu_snapshot_dir=str(root / "snapshots"),
            qemu_ssh_key_path=str(root / "runner_key"),
        )
    )


def xml(workspace_id: str, disk: Path) -> str:
    """Minimal stopped libvirt definition with exclusively run-owned disk."""
    return f'''<domain type="kvm">
<name>opencuria-workspace-{workspace_id}</name><uuid>{workspace_id}</uuid>
<description>OpenCuria isolated empty integration fixture</description>
<memory unit="MiB">128</memory><vcpu>1</vcpu>
<os><type arch="x86_64" machine="pc">hvm</type></os>
<devices><disk type="file" device="disk"><driver name="qemu" type="qcow2"/>
<source file="{disk}"/><target dev="vda" bus="virtio"/></disk></devices>
</domain>'''


async def prepare() -> None:
    """Capture real disk and prove a real dependent overlay prevents deletion."""
    if (run_dir() / "qemu.json").exists():
        raise RuntimeError("QEMU fixture already exists; cleanup it first")
    rt = runtime()
    source_id, dependent_id, image_id = [str(uuid.uuid4()) for _ in range(3)]
    operation_id = str(uuid.uuid4())
    source = rt._disk_path(source_id)
    dependent = rt._disk_path(dependent_id)
    target = rt._managed_image_path(image_id)
    state = {
        "source_id": source_id,
        "dependent_id": dependent_id,
        "image_id": image_id,
        "operation_id": operation_id,
        "source": str(source),
        "dependent": str(dependent),
        "target": str(target),
    }
    save("qemu.json", state)  # save intent before external effects
    command("qemu-img", "create", "-f", "qcow2", str(source), "16M")
    domain = rt._libvirt_conn().defineXML(xml(source_id, source))
    assert domain.UUIDString() == source_id and domain.isActive() == 0
    # Empty qemu-img disk has never had a guest, credentials, or user data written.
    # Check integrity and state before asserting the explicit lower-level proof.
    command("qemu-img", "check", str(source))
    try:
        await rt.create_image_artifact(
            source_id,
            "No proof negative test",
            artifact_id=image_id,
            operation_id=operation_id,
        )
    except RuntimeError as error:
        assert "scrub proof" in str(error)
    else:
        raise AssertionError("Capture accepted without clean proof")
    artifact = await rt.create_image_artifact(
        source_id,
        "Integration empty disk capture (no OS)",
        artifact_id=image_id,
        operation_id=operation_id,
        credential_clean=True,
    )
    info = json.loads(command("qemu-img", "info", "--output=json", str(target)))
    assert (
        not info.get("backing-filename")
        and artifact.size_bytes == target.stat().st_size
    )
    command(
        "qemu-img",
        "create",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        str(target),
        str(dependent),
    )
    child = rt._libvirt_conn().defineXML(xml(dependent_id, dependent))
    assert child.UUIDString() == dependent_id and child.isActive() == 0
    scan = await rt.inventory()
    save("qemu-inventory.json", asdict(scan))
    assert scan.complete, scan.errors
    assert any(str(target) in r.dependencies for r in scan.resources)
    try:
        await rt.delete_image_artifact(image_id)
    except RuntimeError as error:
        assert "physical dependents" in str(error)
    else:
        raise AssertionError("Deletion accepted with real dependent overlay")
    assert target.exists() and dependent.exists()
    save("qemu-artifact.json", asdict(artifact))
    log.info("integration_qemu_capture_dependency_passed", image_id=image_id)


async def cleanup() -> None:
    """Undefine exact UUID-checked stopped fixtures, then delete after overlay gone."""

    state = load("qemu.json")
    rt = runtime()
    for identity, path_key in (
        (state["dependent_id"], "dependent"),
        (state["source_id"], "source"),
    ):
        domains = rt._libvirt_conn().listAllDomains(0)
        domain = next(
            (d for d in domains if d.name() == rt._domain_name(identity)), None
        )
        if domain is not None:
            if domain.UUIDString() != identity or domain.isActive():
                raise RuntimeError("Refusing cleanup: domain identity/state changed")
            disk = Path(state[path_key])
            if str(disk) not in domain.XMLDesc(0):
                raise RuntimeError("Refusing cleanup: fixture disk changed")
            domain.undefineFlags(0)
        path = Path(state[path_key]).resolve()
        if not path.is_relative_to(run_dir() / "qemu" / "disks"):
            raise RuntimeError("Fixture path escaped run directory")
        path.unlink(missing_ok=True)
    await rt.delete_image_artifact(state["image_id"])
    assert not Path(state["target"]).exists()
    assert not Path(state["target"]).with_suffix(".manifest.json").exists()
    log.info("integration_qemu_exact_cleanup_passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "cleanup"])
    args = parser.parse_args()
    asyncio.run(prepare() if args.action == "prepare" else cleanup())
