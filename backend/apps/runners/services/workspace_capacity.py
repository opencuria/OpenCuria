"""Capacity admission shared by workspace creation and lifecycle workflows."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from common.exceptions import ConflictError

from ..enums import TaskType, WorkspaceStatus
from ..exceptions import RunnerOfflineError, WorkspaceStateError
from ..repositories import RunnerRepository, TaskRepository, WorkspaceRepository
from ..workspace_configuration_repository import WorkspaceConfigurationRepository

if TYPE_CHECKING:
    from ..models import Runner, Task, Workspace


class WorkspaceCapacityService:
    """Reserve resources until runtime outcomes and parent workflows are known."""

    @staticmethod
    def previous_resources(workspace: Workspace) -> dict[str, int]:
        """Keep pre-update sizing until the runner confirms a new configuration."""
        command = WorkspaceRepository.last_resource_command(workspace.id)
        return {
            name: WorkspaceCapacityService._reserved_value(
                workspace,
                workspace.runner,
                name,
                command.get("payload", {}),
                command.get("task__status"),
            )
            for name in ("vcpus", "memory_mb", "disk_size_gb")
        }

    @staticmethod
    def _reserved_value(
        workspace: Workspace,
        runner: Runner,
        name: str,
        payload: dict,
        status: str | None,
    ) -> int:
        """Retain physical disk bounds and unresolved pre-update resource sizes."""
        previous = (
            payload.get("_previous_qemu_resources", {}) if status != "completed" else {}
        )
        return max(
            getattr(workspace, f"qemu_{name}")
            or payload.get(f"qemu_{name}")
            or getattr(runner, f"qemu_default_{name}"),
            previous.get(name, 0),
            (payload.get("qemu_disk_size_gb") or 0) if name == "disk_size_gb" else 0,
        )

    @staticmethod
    def ensure(
        runner_id: uuid.UUID,
        *,
        vcpus: int | None,
        memory_mb: int | None,
        disk_size_gb: int | None,
        exclude_workspace_id: uuid.UUID | None = None,
        reserve_compute: bool = True,
    ) -> None:
        """Check committed reservations; allocation callers hold the runner lock.

        A preflight may also call this method, but only the check inside the
        allocation transaction authorizes consuming additional capacity.
        """
        runner = RunnerRepository.get_by_id(runner_id)
        dimensions = (
            ("vcpus", vcpus, "vCPU", ""),
            ("memory_mb", memory_mb, "RAM", " MiB"),
            ("disk_size_gb", disk_size_gb, "disk", " GiB"),
        )
        for name, requested, label, unit in dimensions:
            effective = (
                requested
                if requested is not None
                else getattr(runner, f"qemu_default_{name}")
            )
            minimum = getattr(runner, f"qemu_min_{name}")
            maximum = getattr(runner, f"qemu_max_{name}")
            if not minimum <= effective <= maximum:
                label = "Disk" if name == "disk_size_gb" else label
                raise ConflictError(
                    f"{label} value must be between {minimum} and {maximum}{unit}"
                )
        if all(
            getattr(runner, f"qemu_max_active_{name}") is None
            for name, *_ in dimensions
        ):
            return
        for name, requested, label, unit in dimensions:
            if not reserve_compute and name != "disk_size_gb":
                continue
            limit = getattr(runner, f"qemu_max_active_{name}")
            if limit is None:
                continue
            workspaces = WorkspaceRepository.list_reserved_qemu_by_runner(
                runner_id, include_retained_disk=name == "disk_size_gb"
            )
            default = getattr(runner, f"qemu_default_{name}")
            total = (requested if requested is not None else default) + sum(
                WorkspaceCapacityService._reserved_value(
                    workspace,
                    runner,
                    name,
                    workspace.capacity_payload or {},
                    workspace.capacity_status,
                )
                for workspace in workspaces
                if workspace.id != exclude_workspace_id
            )
            if total > limit:
                raise ConflictError(
                    f"Runner active {label} limit exceeded ({total}/{limit}{unit})"
                )

    @staticmethod
    def resume(workspace_id: uuid.UUID, task_id: uuid.UUID) -> tuple[Workspace, Task]:
        """Recheck capacity and stopped state while reserving the resume task."""
        with WorkspaceConfigurationRepository.updating(workspace_id) as workspace:
            if workspace.current_task_id or workspace.active_operation:
                raise ConflictError("Workspace has an unresolved lifecycle operation")
            if workspace.status != WorkspaceStatus.STOPPED:
                raise WorkspaceStateError("Workspace must be stopped to resume")
            if not workspace.runner.is_online:
                raise RunnerOfflineError(str(workspace.runner_id))
            effective = WorkspaceCapacityService.previous_resources(workspace)
            WorkspaceCapacityService.ensure(
                workspace.runner_id,
                vcpus=effective["vcpus"],
                memory_mb=effective["memory_mb"],
                disk_size_gb=effective["disk_size_gb"],
                exclude_workspace_id=workspace.id,
            )
            workspace = WorkspaceRepository.update_qemu_resources(
                workspace,
                qemu_vcpus=effective["vcpus"],
                qemu_memory_mb=effective["memory_mb"],
                qemu_disk_size_gb=effective["disk_size_gb"],
            )
            task = TaskRepository.create(
                task_id=task_id,
                runner=workspace.runner,
                workspace=workspace,
                task_type=TaskType.RESUME_WORKSPACE,
            )
            return workspace, task
