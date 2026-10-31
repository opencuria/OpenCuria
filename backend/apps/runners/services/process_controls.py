"""User process controls, separate from agent session-scoped process execution.

Users see persistent processes and running temporary processes. They may stop
temporary processes, but may not restart or delete their agent history. Runner
RPCs and persistence remain with RunnerService; this service owns user policy.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from common.exceptions import NotFoundError, ServiceError

from . import RunnerService, get_runner_service

if TYPE_CHECKING:
    from apps.accounts.models import User

    from ..models import WorkspaceProcess


class TemporaryProcessControlError(ServiceError):
    """A user attempted a control reserved for the temporary process's agent."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="validation_error")


class ProcessControlService:
    """Authorize workspace-owner controls and apply user process policy."""

    def __init__(self, runners: RunnerService | None = None) -> None:
        """Inject process execution and workspace authorization."""
        self.runners = runners or get_runner_service()

    async def _require_workspace(
        self, workspace_id: uuid.UUID, *, user: User, organization_id: uuid.UUID
    ) -> None:
        """Require the active organization's workspace owner before any lookup."""
        try:
            await sync_to_async(self.runners.get_workspace_for_user)(
                workspace_id, user=user, organization_id=organization_id
            )
        except NotFoundError:
            raise NotFoundError("Workspace", str(workspace_id))

    async def list_processes(
        self,
        workspace_id: uuid.UUID,
        *,
        user: User,
        organization_id: uuid.UUID,
        live: bool = False,
    ) -> list[WorkspaceProcess]:
        """Return persistent processes and running temps, retaining agent history."""
        await self._require_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        processes = await self.runners.list_processes(workspace_id, live=live)
        return [
            process
            for process in processes
            if str(getattr(process, "kind", "") or "") != "temp"
            or str(getattr(process, "status", "") or "") == "running"
        ]

    async def stop_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        user: User,
        organization_id: uuid.UUID,
    ) -> WorkspaceProcess:
        """Stop a persistent or temporary process without deleting history."""
        await self._require_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        return await self.runners.stop_process(workspace_id, id_or_name)

    async def restart_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        user: User,
        organization_id: uuid.UUID,
    ) -> WorkspaceProcess:
        """Restart a persistent process using its stored command and workdir."""
        await self._require_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        existing = await self.runners.get_process(workspace_id, id_or_name)
        if str(getattr(existing, "kind", "") or "") == "temp":
            raise TemporaryProcessControlError(
                "Temporary processes cannot be restarted by users; "
                "they live only for their agent session."
            )
        return await self.runners.restart_process(workspace_id, id_or_name, user=user)

    async def delete_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        user: User,
        organization_id: uuid.UUID,
    ) -> uuid.UUID:
        """Delete a persistent process, stopping it first when necessary."""
        await self._require_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        existing = await self.runners.get_process(workspace_id, id_or_name)
        if str(getattr(existing, "kind", "") or "") == "temp":
            raise TemporaryProcessControlError(
                "Temporary processes cannot be deleted by users; stop them instead."
            )
        return await self.runners.delete_process(workspace_id, id_or_name)
