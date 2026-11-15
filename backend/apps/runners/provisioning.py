"""Runner provisioning command for a workspace on one concrete image version.

Shared by workspace creation, captured-image clones and recreate so every
path provisions an image version identically. Payloads never contain secrets;
credentials are resolved at delivery time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import TaskType
from .guest_os import image_minimums


@dataclass(frozen=True)
class ProvisioningCommand:
    """Task type, Socket.IO event and secret-free payload for one provisioning."""

    task_type: str
    event: str
    payload: dict


def uses_artifact_clone(image) -> bool:
    """Published QEMU captures are cloned by artifact id, not by image path."""
    return image.origin_type == "workspace_capture"


def disk_size_for(requested_gb: int | None, image) -> int | None:
    """A workspace disk must hold its image's virtual disk and fit its guest OS.

    Without a request the image's virtual disk size (if known) is returned and
    resource resolution applies the guest OS minimum to the runner default.
    """
    minimum = getattr(image, "min_disk_size_gb", None)
    if requested_gb is None:
        return minimum
    floor = image_minimums(getattr(image, "guest_os", None), minimum)
    return max(requested_gb, floor.disk_size_gb)


def provisioning_command(
    *,
    workspace_id,
    workspace_name: str,
    image,
    repos: list[str],
    qemu_vcpus: int | None,
    qemu_memory_mb: int | None,
    qemu_disk_size_gb: int | None,
) -> ProvisioningCommand:
    """Build the provisioning command for ``image``."""
    qemu = {
        "qemu_vcpus": qemu_vcpus,
        "qemu_memory_mb": qemu_memory_mb,
        "qemu_disk_size_gb": qemu_disk_size_gb,
    }
    if uses_artifact_clone(image):
        return ProvisioningCommand(
            TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT,
            "task:create_workspace_from_image_artifact",
            {
                "workspace_id": str(workspace_id),
                "workspace_name": workspace_name,
                "image_artifact_id": image.runner_ref,
                **qemu,
            },
        )
    return ProvisioningCommand(
        TaskType.CREATE_WORKSPACE,
        "task:create_workspace",
        {
            "workspace_id": str(workspace_id),
            "repos": list(repos),
            "workspace_name": workspace_name,
            **qemu,
            "configure_commands": [],
            "image_artifact_id": str(image.id),
            "base_image_path": image.runner_ref,
        },
    )
