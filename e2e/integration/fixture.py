"""Seed auth; attach actual Docker containers to HTTP-built generations.

Run with backend Python and integration Django settings. No inventory or metrics
are seeded: the standard runner must observe every physical resource itself.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import uuid
from pathlib import Path

import django
import structlog
from support import command, load, run_dir, save

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
django.setup()

from apps.accounts.models import User
from apps.organizations.models import Membership, Organization
from apps.runners.models import ImageBuildJob, Runner, Workspace

log = structlog.get_logger()


def seed() -> None:
    """Create only this run's tenant and a fixed integration login."""
    if (run_dir() / "manifest.json").exists():
        raise RuntimeError("Run already seeded; choose a new INTEGRATION_RUN_DIR")
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "integration.settings":
        raise RuntimeError("Refusing to seed outside integration overlay")
    password = os.environ["INTEGRATION_ADMIN_PASSWORD"]
    token = os.environ["RUNNER_API_TOKEN"]
    identity = uuid.uuid4().hex
    org = Organization.objects.create(
        name="Runner Image Verification", slug=f"oc-integration-{identity}"
    )
    user, _ = User.objects.get_or_create(
        email="images-admin@example.test",
        defaults={"username": "images-admin@example.test"},
    )
    user.first_name, user.last_name = "Integration", "Admin"
    user.set_password(password)
    user.save()
    Membership.objects.create(user=user, organization=org, role="admin")
    runner = Runner.objects.create(
        name=f"Integration {identity[:8]}",
        organization=org,
        api_token_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    save(
        "manifest.json",
        {
            "run_id": identity,
            "org_id": str(org.id),
            "runner_id": str(runner.id),
            "user_id": str(user.id),
            "workspace_ids": [],
            "container_ids": [],
        },
    )
    log.info("integration_seeded", directory=str(run_dir()), runner_id=str(runner.id))


def attach() -> None:
    """Pin stopped and running real containers to generation one, not latest.

    These are explicit lower-level fixtures, NOT claims of successful agent or
    desktop provisioning. They contain no credential files or terminal sessions.
    """
    manifest = load()
    build = load("build.json")
    job = ImageBuildJob.objects.get(
        image_definition_id=build["definition_id"], runner_id=manifest["runner_id"]
    )
    image = job.current_generation
    if image is None or image.status != "ready":
        raise RuntimeError("HTTP build not ready")
    if manifest["workspace_ids"]:
        raise RuntimeError("Fixtures already attached")
    physical = command(
        "docker", "image", "inspect", "--format", "{{.Id}}", image.runner_ref
    ).strip()
    manifest.update(
        image_id=str(image.id), image_ref=image.runner_ref, physical_image_id=physical
    )
    save("manifest.json", manifest)
    for running in (False, True):
        workspace_id = str(uuid.uuid4())
        # Record intent before creating any external resource (cleanup after failures).
        manifest["workspace_ids"].append(workspace_id)
        save("manifest.json", manifest)
        container = command(
            "docker",
            "create",
            "--name",
            f"opencuria-workspace-{workspace_id}",
            "--label",
            f"opencuria.workspace-id={workspace_id}",
            "--label",
            f"oc.integration.run={manifest['run_id']}",
            physical,
            "tail",
            "-f",
            "/dev/null",
        ).strip()
        manifest["container_ids"].append(container)
        save("manifest.json", manifest)
        if running:
            command("docker", "start", container)
        Workspace.objects.create(
            id=workspace_id,
            runner_id=manifest["runner_id"],
            created_by_id=manifest["user_id"],
            base_image_instance=image,
            name=f"Integration {'running' if running else 'stopped'} physical fixture",
            status="running" if running else "stopped",
            credentials_present=False,
        )
    log.info("integration_containers_attached", image_id=str(image.id))


def attach_qemu() -> None:
    """Register actual lower-level captured bytes for runner/browser inventory."""
    from apps.runners.models import ImageInstance

    manifest, state = load(), load("qemu.json")
    source = Workspace.objects.create(
        id=state["source_id"],
        runner_id=manifest["runner_id"],
        created_by_id=manifest["user_id"],
        runtime_type="qemu",
        status="stopped",
        name="Integration empty source (no guest OS)",
        credentials_present=False,
    )
    artifact = load("qemu-artifact.json")
    image = ImageInstance.objects.create(
        id=state["image_id"],
        runner_id=manifest["runner_id"],
        runtime_type="qemu",
        origin_type="workspace_capture",
        origin_workspace=source,
        created_by_id=manifest["user_id"],
        name=artifact["name"],
        runner_ref=state["target"],
        size_bytes=artifact["size_bytes"],
        status="ready",
    )
    Workspace.objects.create(
        id=state["dependent_id"],
        runner_id=manifest["runner_id"],
        created_by_id=manifest["user_id"],
        runtime_type="qemu",
        status="stopped",
        name="Integration stopped overlay (no guest OS)",
        base_image_instance=image,
        credentials_present=False,
    )
    log.info("integration_qemu_real_identities_registered")


def attach_booted_guest() -> None:
    """Register the exact real guest; never mutate runtime or fabricate inventory."""
    from django.db import transaction
    from apps.runners.models import ImageInstance

    if os.environ.get("DJANGO_SETTINGS_MODULE") != "integration.settings":
        raise RuntimeError("Refusing attachment outside integration overlay")
    manifest, state, proof = load(), load("guest.json"), load("guest-result.json")
    assert proof["workspace_id"] == state["workspace_id"]
    assert proof["external_stop_observed"] and proof["scrub_checkpoint_verified"]
    base = Path(state["base"]).resolve()
    root = base.parent.parent
    assert root.parent == Path("/var/lib/libvirt/images/opencuria-integration")
    uuid.UUID(root.name)
    assert base == root / "images" / "full-guest-base.qcow2"
    for key, env, subdir in (
        ("disk_dir", "RUNNER_QEMU_DISK_DIR", "disks"),
        ("snapshot_dir", "RUNNER_QEMU_SNAPSHOT_DIR", "snapshots"),
    ):
        assert Path(state[key]).resolve() == root / subdir
        assert Path(os.environ[env]).resolve() == root / subdir
    assert Path(os.environ["RUNNER_QEMU_IMAGE_CACHE_DIR"]).resolve() == base.parent
    assert base.is_file()
    domain = "opencuria-workspace-" + str(uuid.UUID(state["workspace_id"]))
    assert command("virsh", "-c", "qemu:///system", "domstate", domain).strip() == "shut off"
    disks = command("virsh", "-c", "qemu:///system", "domblklist", domain)
    assert str(root / "disks" / (state["workspace_id"] + ".qcow2")) in disks
    with transaction.atomic():
        if Workspace.objects.filter(pk=state["workspace_id"]).exists():
            raise RuntimeError("Guest already attached; refusing overwrite")
        base_image = ImageInstance.objects.create(
            runner_id=manifest["runner_id"], runtime_type="qemu",
            origin_type="workspace_capture", is_legacy=True,
            created_by_id=manifest["user_id"], name="Integration pristine guest base (legacy import)",
            runner_ref=str(base), status="ready", size_bytes=base.stat().st_size,
        )
        Workspace.objects.create(
            id=state["workspace_id"], runner_id=manifest["runner_id"],
            created_by_id=manifest["user_id"], runtime_type="qemu",
            status="stopped", credentials_present=True,
            base_image_instance=base_image, name="Integration real booted Ubuntu guest",
            qemu_vcpus=1, qemu_memory_mb=1024, qemu_disk_size_gb=20,
        )
    save("guest-attachment.json", {"workspace_id": state["workspace_id"],
                                 "base_image_id": str(base_image.id), "base": str(base)})
    log.info("integration_booted_guest_attached", workspace_id=state["workspace_id"])


def cleanup() -> None:
    """Remove only recorded, label-verified fixtures; never prune global resources.

    Leave DB lifecycle history and built images for evidence. HTTP force deletion
    removes builds. Failed builds may be removed manually by exact inspected tags.
    """
    manifest = load()
    for workspace_id in manifest["workspace_ids"]:
        name = f"opencuria-workspace-{workspace_id}"
        result = command("docker", "ps", "-aq", "--filter", f"name=^/{name}$").strip()
        if not result:
            continue
        label = command(
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "oc.integration.run"}}',
            result,
        ).strip()
        if label != manifest["run_id"]:
            raise RuntimeError("Container ownership mismatch; refusing cleanup")
        command("docker", "rm", "-f", result)
    log.info("integration_fixture_containers_cleaned")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seed", "attach", "attach-qemu", "attach-booted-guest", "cleanup"])
    {"seed": seed, "attach": attach, "attach-qemu": attach_qemu, "attach-booted-guest": attach_booted_guest, "cleanup": cleanup}[
        parser.parse_args().action
    ]()
