"""Admit live work on a workspace under its lifecycle locks and fences.

Runner then workspace locks serialize admission against lifecycle operations
(capture, reset, auto-stop). Other contexts reach these checks through
``WorkspaceAdmissionService`` and pass their own reservation as a callback
that runs under the same locks.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.db import transaction
from django.db.models import F

from common.exceptions import ConflictError

from .locking import lock_runner
from .models import Workspace


class WorkspaceAdmissionRepository:
    """Lock-ordered admission checks on persisted workspace lifecycle state."""

    @staticmethod
    def ensure_interactions_available(workspace_id: uuid.UUID) -> None:
        """Guard live chat access using persisted state, not cached relations.

        Saved history readers deliberately do not call this guard.
        """
        workspace = Workspace.objects.get(id=workspace_id)
        if workspace.active_operation == "capturing_image":
            raise ConflictError("Workspace is currently capturing image")
        if workspace.active_operation in {"resetting", "updating"}:
            raise ConflictError(f"Workspace is currently {workspace.active_operation}")
        if workspace.current_task_id and not workspace.active_operation:
            raise ConflictError(
                "Workspace recovery is waiting for the runner to verify its state"
            )
        if workspace.status == "failed":
            raise ConflictError(
                "Workspace runtime is unavailable; reset or delete the workspace"
            )

    @staticmethod
    def admit_run(
        workspace_id: uuid.UUID,
        *,
        scheduled: bool,
        reserve: Callable[[], bool],
    ) -> bool:
        """Run *reserve* under runner then workspace write locks.

        The workspace row serializes the short admission transaction. A
        scheduled launch also requires a running workspace without a lifecycle
        operation and returns False otherwise. The interaction fence raises
        ConflictError before *reserve* runs.
        """
        with transaction.atomic():
            # Acquire the workspace write lock before reading eligibility. This
            # serializes scheduled/manual launch admission across all sessions,
            # including on SQLite where select_for_update() is unavailable.
            runner_id = Workspace.objects.values_list("runner_id", flat=True).get(
                id=workspace_id
            )
            lock_runner(runner_id)
            workspace_query = Workspace.objects.filter(id=workspace_id)
            if scheduled:
                workspace_query = workspace_query.filter(
                    status="running",
                    active_operation__isnull=True,
                )
            reserved_workspace = workspace_query.update(updated_at=F("updated_at"))
            if not reserved_workspace:
                return False
            WorkspaceAdmissionRepository.ensure_interactions_available(workspace_id)
            return reserve()

    @staticmethod
    def run_locked(workspace_id: uuid.UUID, action: Callable[[], bool]) -> bool:
        """Run *action* under runner then workspace row locks.

        Shares admission's serialization without applying the interaction
        fence. Returns False without calling *action* when the workspace is
        missing.
        """
        runner_id = (
            Workspace.objects.filter(pk=workspace_id)
            .values_list("runner_id", flat=True)
            .first()
        )
        if runner_id is None:
            return False
        with transaction.atomic():
            lock_runner(runner_id)
            Workspace.objects.select_for_update().get(pk=workspace_id)
            return action()
