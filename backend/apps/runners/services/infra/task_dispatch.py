"""Workspace task-dispatch helpers (Phase 1 infra mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services.py``. No logic changes; the mixin relies on the
facade (``RunnerService``) providing ``self.workspaces``, ``self.tasks``
and ``_emit_to_runner`` / ``_forward_workspace_operation``.
"""

from __future__ import annotations

import logging
import uuid

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ...enums import WorkspaceOperation

logger = logging.getLogger(__name__)


class TaskDispatchMixin:
    """Workspace task dispatch helpers shared by RunnerService."""

    async def _set_workspace_operation(
        self,
        workspace: "Workspace",
        operation: WorkspaceOperation | None,
    ) -> "Workspace":
        """Persist and broadcast a workspace operation change."""
        workspace = await sync_to_async(self.workspaces.update_active_operation)(
            workspace,
            operation,
        )
        self._forward_workspace_operation(str(workspace.id), workspace.active_operation)
        return workspace

    async def _dispatch_workspace_task(
        self,
        *,
        runner: "Runner",
        event: str,
        payload: dict,
        task: "Task",
        workspace: "Workspace" | None = None,
        operation: WorkspaceOperation | None = None,
    ) -> None:
        """Set busy state, emit the task to the runner, and roll back on dispatch failure."""
        try:
            if workspace is not None and operation is not None:
                await self._set_workspace_operation(workspace, operation)
            await self._emit_to_runner(runner, event, payload)
            await sync_to_async(self.tasks.mark_in_progress)(task)
        except Exception as exc:
            if workspace is not None and operation is not None:
                await self._set_workspace_operation(workspace, None)
            await sync_to_async(self.tasks.fail)(task, str(exc))
            raise

    @staticmethod
    def _resolve_qemu_resources(
        *,
        runner: "Runner",
        qemu_vcpus: int | None,
        qemu_memory_mb: int | None,
        qemu_disk_size_gb: int | None,
        current: tuple[int, int, int] | None = None,
    ) -> tuple[int, int, int]:
        """Resolve effective QEMU resources and validate against runner limits."""
        current_vcpus = current[0] if current else runner.qemu_default_vcpus
        current_memory_mb = current[1] if current else runner.qemu_default_memory_mb
        current_disk_size_gb = (
            current[2] if current else runner.qemu_default_disk_size_gb
        )

        resolved_vcpus = qemu_vcpus if qemu_vcpus is not None else current_vcpus
        resolved_memory_mb = (
            qemu_memory_mb if qemu_memory_mb is not None else current_memory_mb
        )
        resolved_disk_size_gb = (
            qemu_disk_size_gb if qemu_disk_size_gb is not None else current_disk_size_gb
        )

        if not (runner.qemu_min_vcpus <= resolved_vcpus <= runner.qemu_max_vcpus):
            raise ConflictError(
                f"vCPU value must be between {runner.qemu_min_vcpus} and {runner.qemu_max_vcpus}"
            )
        if not (
            runner.qemu_min_memory_mb <= resolved_memory_mb <= runner.qemu_max_memory_mb
        ):
            raise ConflictError(
                f"RAM value must be between {runner.qemu_min_memory_mb} and {runner.qemu_max_memory_mb} MiB"
            )
        if not (
            runner.qemu_min_disk_size_gb
            <= resolved_disk_size_gb
            <= runner.qemu_max_disk_size_gb
        ):
            raise ConflictError(
                f"Disk value must be between {runner.qemu_min_disk_size_gb} and {runner.qemu_max_disk_size_gb} GiB"
            )
        return resolved_vcpus, resolved_memory_mb, resolved_disk_size_gb

    async def _ensure_qemu_active_capacity(
        self,
        *,
        runner: "Runner",
        requested_vcpus: int,
        requested_memory_mb: int,
        requested_disk_size_gb: int,
        exclude_workspace_id: uuid.UUID | None = None,
    ) -> None:
        """Ensure active QEMU aggregate limits allow the requested resources."""
        active_qemu = await sync_to_async(list)(
            self.workspaces.list_running_qemu_by_runner(runner.id)
        )

        total_vcpus = 0
        total_memory_mb = 0
        total_disk_size_gb = 0
        for ws in active_qemu:
            if exclude_workspace_id and ws.id == exclude_workspace_id:
                continue
            total_vcpus += ws.qemu_vcpus or runner.qemu_default_vcpus
            total_memory_mb += ws.qemu_memory_mb or runner.qemu_default_memory_mb
            total_disk_size_gb += (
                ws.qemu_disk_size_gb or runner.qemu_default_disk_size_gb
            )

        next_total_vcpus = total_vcpus + requested_vcpus
        next_total_memory_mb = total_memory_mb + requested_memory_mb
        next_total_disk_size_gb = total_disk_size_gb + requested_disk_size_gb

        if (
            runner.qemu_max_active_vcpus is not None
            and next_total_vcpus > runner.qemu_max_active_vcpus
        ):
            raise ConflictError(
                f"Runner active vCPU limit exceeded ({next_total_vcpus}/{runner.qemu_max_active_vcpus})"
            )
        if (
            runner.qemu_max_active_memory_mb is not None
            and next_total_memory_mb > runner.qemu_max_active_memory_mb
        ):
            raise ConflictError(
                f"Runner active RAM limit exceeded ({next_total_memory_mb}/{runner.qemu_max_active_memory_mb} MiB)"
            )
        if (
            runner.qemu_max_active_disk_size_gb is not None
            and next_total_disk_size_gb > runner.qemu_max_active_disk_size_gb
        ):
            raise ConflictError(
                f"Runner active disk limit exceeded ({next_total_disk_size_gb}/{runner.qemu_max_active_disk_size_gb} GiB)"
            )

