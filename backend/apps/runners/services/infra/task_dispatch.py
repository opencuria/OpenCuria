"""Workspace task-dispatch helpers (Phase 1 infra mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services.py``. No logic changes; the mixin relies on the
facade (``RunnerService``) providing ``self.workspaces``, ``self.tasks``
and ``_emit_to_runner`` / ``_forward_workspace_operation``.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ...enums import WorkspaceOperation

if TYPE_CHECKING:
    from ...models import Runner, Task, Workspace

logger = logging.getLogger(__name__)


class TaskDispatchMixin:
    """Workspace task dispatch helpers shared by RunnerService."""

    async def _set_workspace_operation(
        self,
        workspace: Workspace,
        operation: WorkspaceOperation | None,
    ) -> Workspace:
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
        runner: Runner,
        event: str,
        payload: dict,
        task: Task,
        workspace: Workspace | None = None,
        operation: WorkspaceOperation | None = None,
    ) -> None:
        """Broadcast the reserved operation and deliver its durable command."""
        if workspace is not None and operation is not None:
            workspace.active_operation = operation
            self._forward_workspace_operation(str(workspace.id), operation)
        await self._emit_to_runner(runner, event, payload)
        await sync_to_async(self.tasks.mark_in_progress)(task)

    @staticmethod
    def _resolve_qemu_resources(
        *,
        runner: Runner,
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
                f"vCPU value must be between {runner.qemu_min_vcpus} "
                f"and {runner.qemu_max_vcpus}"
            )
        if not (
            runner.qemu_min_memory_mb <= resolved_memory_mb <= runner.qemu_max_memory_mb
        ):
            raise ConflictError(
                f"RAM value must be between {runner.qemu_min_memory_mb} "
                f"and {runner.qemu_max_memory_mb} MiB"
            )
        if not (
            runner.qemu_min_disk_size_gb
            <= resolved_disk_size_gb
            <= runner.qemu_max_disk_size_gb
        ):
            raise ConflictError(
                f"Disk value must be between {runner.qemu_min_disk_size_gb} "
                f"and {runner.qemu_max_disk_size_gb} GiB"
            )
        return resolved_vcpus, resolved_memory_mb, resolved_disk_size_gb

    async def _ensure_qemu_active_capacity(
        self,
        *,
        runner: Runner,
        requested_vcpus: int,
        requested_memory_mb: int,
        requested_disk_size_gb: int,
        exclude_workspace_id: uuid.UUID | None = None,
    ) -> None:
        """Ensure active QEMU aggregate limits allow the requested resources."""
        from ..workspace_capacity import WorkspaceCapacityService

        await sync_to_async(WorkspaceCapacityService.ensure)(
            runner.id,
            vcpus=requested_vcpus,
            memory_mb=requested_memory_mb,
            disk_size_gb=requested_disk_size_gb,
            exclude_workspace_id=exclude_workspace_id,
        )
