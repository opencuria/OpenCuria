"""Read and release workspace reservations without importing their coordinators.

Task admission, persisted operation updates and frontend projections share this
read boundary. The capture and recreate services own phase progression
and depend on these reads, never the other way round.
"""

from __future__ import annotations

import uuid

from .models import (
    CaptureRequest,
    ImageDeletionRequest,
    LifecycleCommand,
    Task,
    Workspace,
    WorkspaceRecreateRequest,
)

REQUEST_TERMINAL_PHASES = ["completed", "failed"]
OPERATION_BY_REASON = {
    WorkspaceRecreateRequest.Reason.CAPTURE: "capturing_image",
    WorkspaceRecreateRequest.Reason.RESET: "resetting",
    WorkspaceRecreateRequest.Reason.UPDATE: "updating",
}


class WorkspaceOperationRepository:
    """Read reservation ownership and the committed blocking operation."""

    @staticmethod
    def release_workspace(task: Task) -> None:
        """Release only this child, preserving its enclosing durable workflow."""
        operation = (
            WorkspaceOperationRepository.project(task.workspace_id, None)
            if task.workspace_id
            else None
        )
        Workspace.objects.filter(current_task=task).update(
            current_task=None, active_operation=operation
        )

    @staticmethod
    def forced_deletion_approvals(organization_id: uuid.UUID) -> list[dict]:
        """Load live forced-deletion approvals for admission checks."""
        return list(
            ImageDeletionRequest.objects.filter(
                organization_id=organization_id, mode="force"
            )
            .exclude(phase__in=["completed", "cancelled"])
            .values_list("approval", flat=True)
        )

    @staticmethod
    def credentials_synchronizing(workspace_id: uuid.UUID) -> bool:
        """Whether credential injection is pending or executing."""
        return Task.objects.filter(
            workspace_id=workspace_id,
            type="inject_credentials",
            status__in=["pending", "in_progress"],
        ).exists()

    @staticmethod
    def live_capture(workspace_id: uuid.UUID | str) -> CaptureRequest | None:
        """Return the unfinished capture reservation, if any."""
        return (
            CaptureRequest.objects.filter(workspace_id=workspace_id)
            .exclude(phase__in=REQUEST_TERMINAL_PHASES)
            .first()
        )

    @staticmethod
    def capture_active(workspace_id: uuid.UUID | str) -> bool:
        """Whether a capture owns the workspace, including between children."""
        return (
            CaptureRequest.objects.filter(workspace_id=workspace_id)
            .exclude(phase__in=REQUEST_TERMINAL_PHASES)
            .exists()
        )

    @staticmethod
    def live_recreate(workspace_id: uuid.UUID | str) -> WorkspaceRecreateRequest | None:
        """Return the unfinished recreate reservation, if any."""
        return (
            WorkspaceRecreateRequest.objects.filter(workspace_id=workspace_id)
            .exclude(phase__in=REQUEST_TERMINAL_PHASES)
            .first()
        )

    @staticmethod
    def recreate_active(workspace_id: uuid.UUID | str) -> bool:
        """Whether a recreate owns the workspace, including between children."""
        return WorkspaceOperationRepository.live_recreate(workspace_id) is not None

    @staticmethod
    def owns_recreate_child(task_id: uuid.UUID | str) -> bool:
        """Whether the exact task is a child of an unfinished recreate."""
        return (
            WorkspaceRecreateRequest.objects.filter(child_id=task_id)
            .exclude(phase__in=REQUEST_TERMINAL_PHASES)
            .exists()
        )

    @staticmethod
    def project(workspace_id: uuid.UUID | str, fallback: str | None) -> str | None:
        """Project parent operations while retaining current-child interventions.

        A capture takes precedence over a recreate. Without a durable parent,
        the caller's fallback applies even if an old child needs intervention.
        """
        recreate = WorkspaceOperationRepository.live_recreate(workspace_id)
        operation = fallback
        if recreate is not None:
            operation = (
                None
                if WorkspaceOperationRepository._has_intervening_child(workspace_id)
                else OPERATION_BY_REASON[recreate.reason]
            )
        if not WorkspaceOperationRepository.capture_active(workspace_id):
            return operation
        if WorkspaceOperationRepository._has_intervening_child(workspace_id):
            return None
        return "capturing_image"

    @staticmethod
    def _has_intervening_child(workspace_id: uuid.UUID | str) -> bool:
        """Only the workspace's current child can fence its parent operation."""
        return LifecycleCommand.objects.filter(
            task__workspace_id=workspace_id,
            task_id=Workspace.objects.filter(pk=workspace_id).values("current_task_id")[
                :1
            ],
            phase__in={"intervention", "attention_required"},
        ).exists()
