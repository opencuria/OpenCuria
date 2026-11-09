"""Workspace admission for live work owned by other contexts.

The harness gates chat interactions and reserves runs here instead of reading
workspace lifecycle state or taking runner locks itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..workspace_admission_repository import WorkspaceAdmissionRepository

if TYPE_CHECKING:
    from ..models import Workspace


class WorkspaceAdmissionService:
    """Gate live interactions and serialize run admission per workspace."""

    def __init__(
        self, *, repository: type[WorkspaceAdmissionRepository] | None = None
    ) -> None:
        """Inject the admission repository without constructing runner execution."""
        self.repository = repository or WorkspaceAdmissionRepository

    def ensure_interactions_available(self, workspace_id: uuid.UUID) -> None:
        """Raise ConflictError while a lifecycle operation blocks live work."""
        self.repository.ensure_interactions_available(workspace_id)

    def interaction_target(
        self, workspace_id: uuid.UUID, *, fenced: bool = True
    ) -> Workspace | None:
        """Return the workspace and its runner for one live runner call.

        Re-reads persisted state on every call so each call routes to the
        runner's current session and honors the interaction fence (skipped only
        with ``fenced=False``). Returns None when the workspace is gone.
        """
        return self.repository.interaction_target(workspace_id, fenced=fenced)

    def admit_run(
        self,
        workspace_id: uuid.UUID,
        *,
        reserve: Callable[[], bool],
        scheduled: bool = False,
    ) -> bool:
        """Run *reserve* under the workspace admission locks and fence.

        Returns False when a scheduled launch finds the workspace not running
        or busy with a lifecycle operation, otherwise the result of *reserve*.
        """
        return self.repository.admit_run(
            workspace_id, scheduled=scheduled, reserve=reserve
        )

    def run_locked(self, workspace_id: uuid.UUID, action: Callable[[], bool]) -> bool:
        """Run *action* under the admission locks without the interaction fence.

        Lets a creator roll back its own unstarted work during a lifecycle
        operation. Returns False when the workspace is missing.
        """
        return self.repository.run_locked(workspace_id, action)
