"""Runner provisioning command for a workspace on one concrete image version.

Shared by workspace creation, captured-image clones and recreate so every
path provisions an image version identically. Payloads never contain secrets;
credentials are resolved at delivery time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import RuntimeType, TaskType


@dataclass(frozen=True)
class ProvisioningCommand:
    """Task type, Socket.IO event and secret-free payload for one provisioning."""

    task_type: str
    event: str
    payload: dict


def uses_artifact_clone(image) -> bool:
    """Published QEMU captures are cloned by artifact id, not by image path."""
    return (
        image.origin_type == "workspace_capture"
        and image.runtime_type == RuntimeType.QEMU
    )


def disk_size_for(requested_gb: int | None, image) -> int | None:
    """A workspace disk must be at least as large as the captured version."""
    minimum = getattr(image, "min_disk_size_gb", None)
    if requested_gb is None or minimum is None:
        return requested_gb if requested_gb is not None else minimum
    return max(requested_gb, minimum)


def provisioning_command(
    *,
    workspace_id,
    workspace_name: str,
    image,
    runtime_type: str,
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
                "runtime_type": runtime_type,
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
            "runtime_type": runtime_type,
            **qemu,
            "configure_commands": [],
            "image_artifact_id": str(image.id),
            "image_tag": image.runner_ref if runtime_type == RuntimeType.DOCKER else "",
            "base_image_path": image.runner_ref
            if runtime_type == RuntimeType.QEMU
            else "",
        },
    )
