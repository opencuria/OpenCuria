"""Workspace admission for live work owned by other contexts.

The harness gates chat interactions and reserves runs here instead of reading
workspace lifecycle state or taking runner locks itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from ..workspace_admission_repository import WorkspaceAdmissionRepository


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
