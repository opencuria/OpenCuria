"""Workspace/runner ownership guards (Phase 1 infra mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services.py``. No logic changes; the mixin relies on the
facade (``RunnerService``) providing ``self.workspaces``,
``self.processes`` and sibling helpers such as
``_workspace_operation_label`` / ``_desktop_workspace_runner``.
"""

from __future__ import annotations

import logging
import uuid

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ...enums import WorkspaceStatus
from ...exceptions import RunnerOfflineError, WorkspaceStateError

logger = logging.getLogger(__name__)


class OwnershipMixin:
    """Ownership/availability guards shared by RunnerService."""

    def _ensure_workspace_available(self, workspace: "Workspace") -> None:
        """Reject mutating operations while another blocking lifecycle action runs."""
        if workspace.status in (
            WorkspaceStatus.PENDING_DELETION,
            WorkspaceStatus.DELETING,
            WorkspaceStatus.DELETED,
        ):
            raise ConflictError(
                f"Workspace '{workspace.id}' is pending deletion and cannot be modified"
            )
        if workspace.active_operation:
            raise ConflictError(
                f"Workspace '{workspace.id}' is currently {self._workspace_operation_label(workspace.active_operation)}"
            )

    @staticmethod
    def _ensure_runner_supports_runtime(
        *,
        runner: "Runner",
        runtime_type: str,
    ) -> None:
        """Raise when a runner does not advertise support for a runtime."""
        if runtime_type not in (runner.available_runtimes or []):
            raise ConflictError(f"Runner does not support runtime '{runtime_type}'")

    def _validate_task_runner(self, task, runner_id: str | None) -> bool:
        """Return True if the task belongs to the given runner.

        When *runner_id* is None the check is skipped (e.g. in tests).
        Logs a warning and returns False on ownership mismatch.
        """
        if runner_id is None:
            return True
        if str(task.runner_id) != runner_id:
            logger.warning(
                "Event rejected: task %s belongs to runner %s, not %s",
                task.id,
                task.runner_id,
                runner_id,
            )
            return False
        return True

    async def _desktop_event_owned_by_runner(
        self, workspace_id: str, runner_id: str | None
    ) -> bool:
        """Return True when *runner_id* owns the desktop *workspace_id*.

        Async helper: the cache fast-path rejects a known mismatch
        synchronously, while first matching/empty cache entries validate
        the real ``Workspace.runner_id`` via ``sync_to_async`` — Django
        ORM must never run on the Socket.IO event loop. Missing workspaces
        and missing/invalid ids are rejected. No
        ``DJANGO_ALLOW_ASYNC_UNSAFE`` dependency.
        """
        if not runner_id or not workspace_id:
            logger.warning(
                "desktop event rejected: missing workspace_id or runner_id",
            )
            return False
        try:
            workspace_uuid = uuid.UUID(str(workspace_id))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "desktop event rejected: invalid workspace_id %s",
                workspace_id,
            )
            return False
        try:
            claimed_runner_id = uuid.UUID(str(runner_id))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "desktop event rejected: invalid runner_id %s",
                runner_id,
            )
            return False
        cached = self._desktop_workspace_runner.get(workspace_id)
        if cached is not None and cached != runner_id:
            logger.warning(
                "desktop event rejected: workspace %s is owned by "
                "runner %s, not %s",
                workspace_id,
                cached,
                runner_id,
            )
            return False
        owner_id = await sync_to_async(self.workspaces.get_runner_id)(
            workspace_uuid
        )
        if owner_id is None:
            logger.warning(
                "desktop event rejected: workspace %s not found",
                workspace_id,
            )
            return False
        if owner_id != claimed_runner_id:
            logger.warning(
                "desktop event rejected: workspace %s not owned by %s",
                workspace_id,
                runner_id,
            )
            return False
        return True

    def _ensure_process_dispatchable(self, workspace: "Workspace") -> "Runner":
        """Validate that a process RPC may be dispatched (sync).

        Processes never set ``active_operation``; only deletion states and
        non-running status block dispatch.
        """
        if workspace.status in (
            WorkspaceStatus.PENDING_DELETION,
            WorkspaceStatus.DELETING,
            WorkspaceStatus.DELETED,
        ):
            raise WorkspaceStateError(
                f"Workspace '{workspace.id}' is pending deletion and "
                "cannot run background processes"
            )
        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace.id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' for background processes"
            )
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        return runner

    def _ensure_git_dispatchable(self, workspace: "Workspace") -> "Runner":
        """Validate that a git RPC may be dispatched (sync).

        Git never touches ``active_operation`` for dispatch bookkeeping,
        but it must not run while a blocking lifecycle operation holds
        the workspace (create/start/stop/restart/remove/capture) and it
        must not run on deleting/deleted states. Only ``RUNNING``
        workspaces accept git operations; the runner serialises
        concurrent git ops per workspace/repo behind its own lock.
        """
        if workspace.status in (
            WorkspaceStatus.PENDING_DELETION,
            WorkspaceStatus.DELETING,
            WorkspaceStatus.DELETED,
        ):
            raise WorkspaceStateError(
                f"Workspace '{workspace.id}' is pending deletion and "
                "cannot run git operations"
            )
        if workspace.active_operation:
            raise WorkspaceStateError(
                f"Workspace '{workspace.id}' is currently "
                f"{self._workspace_operation_label(workspace.active_operation)}"
            )
        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace.id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' for git operations"
            )
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        return runner

    def _validate_harness_workspace_runner(
        self,
        workspace_id: uuid.UUID,
        runner_id: str,
    ) -> bool:
        """Return True when *workspace_id* belongs to *runner_id* (sync)."""
        try:
            claimed_runner_id = uuid.UUID(str(runner_id))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "harness reply rejected: invalid runner_id %s",
                runner_id,
            )
            return False
        owner_id = self.workspaces.get_runner_id(workspace_id)
        if owner_id is None:
            logger.warning(
                "harness reply rejected: workspace %s not found",
                workspace_id,
            )
            return False
        if owner_id != claimed_runner_id:
            logger.warning(
                "harness reply rejected: workspace %s not owned by %s",
                workspace_id,
                runner_id,
            )
            return False
        return True

