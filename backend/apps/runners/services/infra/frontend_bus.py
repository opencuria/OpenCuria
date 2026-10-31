"""Publish runner and workspace events through the frontend Socket.IO transport."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync, sync_to_async
from django.db import transaction

from ...enums import WorkspaceStatus
from ...workspace_operation_repository import WorkspaceOperationRepository
from . import realtime

if TYPE_CHECKING:
    from ...models import Runner, Workspace

logger = logging.getLogger(__name__)


class FrontendBusMixin:
    """Frontend event fan-out shared by RunnerService."""

    def _forward_to_frontend(
        self,
        event: str,
        data: dict,
        workspace_id: str,
    ) -> None:
        """
        Schedule a Socket.IO emit to subscribed frontend clients.

        Since service methods are called synchronously (via sync_to_async),
        this schedules the async emit on the running event loop.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            transaction.on_commit(
                lambda: self._emit_frontend_event(event, dict(data), workspace_id)
            )
        else:
            self._emit_frontend_event(event, data, workspace_id)

    def _emit_frontend_event(self, event: str, data: dict, workspace_id: str) -> None:
        """Deliver an event only after its originating transaction committed."""
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                async_to_sync(realtime.emit_to_frontend)(event, data, workspace_id)
            else:
                loop.create_task(realtime.emit_to_frontend(event, data, workspace_id))
        except Exception:
            logger.exception(
                "Failed forwarding event to frontend",
                extra={
                    "event": event,
                    "workspace_id": workspace_id,
                },
            )

    def _forward_runner_status_to_frontend(
        self,
        runner: Runner,
        status: str,
    ) -> None:
        """Emit runner status change events for all workspaces of this runner.

        Sends a ``runner:offline`` or ``runner:online`` event for every
        workspace managed by the runner so subscribed frontend clients can
        update their display without a full page refresh.
        """
        workspaces = self.workspaces.list_for_scope(
            runner_id=runner.id,
            exclude_statuses=(WorkspaceStatus.DELETED, WorkspaceStatus.FAILED),
        )
        event = "runner:offline" if status == "offline" else "runner:online"
        for ws in workspaces:
            ws_id = str(ws.id)
            self._forward_to_frontend(
                event,
                {"workspace_id": ws_id, "runner_id": str(runner.id)},
                ws_id,
            )

    def _forward_workspace_operation(
        self,
        workspace_id: str,
        active_operation: str | None,
    ) -> None:
        """Forward workspace operation changes to subscribed frontend clients."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:

            def publish() -> None:
                payload = self._project_workspace_operation(
                    workspace_id, active_operation
                )
                self._forward_to_frontend(
                    "workspace:operation_changed", payload, workspace_id
                )

            transaction.on_commit(publish)
        else:
            # Only projection crosses the sync boundary. Delivery must stay on
            # this loop, not bounce back from the executor via async_to_sync.
            loop.create_task(
                self._forward_workspace_operation_async(workspace_id, active_operation)
            )

    def _project_workspace_operation(
        self, workspace_id: str, active_operation: str | None
    ) -> dict:
        """Read the committed operation and lifecycle projection synchronously."""
        workspace = self.workspaces.get_by_id(workspace_id)
        operation = WorkspaceOperationRepository.project(workspace_id, active_operation)
        payload = {"workspace_id": workspace_id, "active_operation": operation}
        if workspace is not None:
            payload.update(
                intervention_required=workspace.intervention_required,
                lifecycle_diagnostic=workspace.lifecycle_diagnostic,
            )
        return payload

    async def _forward_workspace_operation_async(
        self, workspace_id: str, active_operation: str | None
    ) -> None:
        """Project off-loop, then schedule delivery on the originating loop."""
        try:
            payload = await sync_to_async(self._project_workspace_operation)(
                workspace_id, active_operation
            )
            self._forward_to_frontend(
                "workspace:operation_changed", payload, workspace_id
            )
        except Exception:
            logger.exception(
                "Failed forwarding workspace operation to frontend",
                extra={"workspace_id": workspace_id},
            )

    def _forward_workspace_status(
        self,
        workspace: Workspace,
        *,
        task_id: str | None = None,
        status: str | None = None,
    ) -> None:
        """Forward workspace status and credential presence to the frontend."""
        payload = {
            "workspace_id": str(workspace.id),
            "status": status or workspace.status,
            "credentials_present": bool(workspace.credentials_present),
        }
        if task_id:
            payload["task_id"] = task_id
        self._forward_to_frontend(
            "workspace:status_changed",
            payload,
            str(workspace.id),
        )

    def _push_process_status(
        self,
        *,
        workspace_id: str,
        process_id: str,
        status: str,
        exit_code: int | None = None,
        pid: int | None = None,
        log_path: str | None = None,
        run_count: int | None = None,
    ) -> None:
        """Forward a background-process status change to the frontend."""
        payload: dict = {
            "workspace_id": workspace_id,
            "process_id": process_id,
            "status": status,
            "exit_code": exit_code,
            "pid": pid,
        }
        if log_path is not None:
            payload["log_path"] = log_path
        if run_count is not None:
            payload["run_count"] = run_count
        self._forward_to_frontend(
            "process:status_changed",
            payload,
            workspace_id,
        )

    def _push_process_removed(
        self,
        *,
        workspace_id: str,
        process_id: str,
    ) -> None:
        """Forward a background-process removal to the frontend."""
        self._forward_to_frontend(
            "process:removed",
            {
                "workspace_id": workspace_id,
                "process_id": process_id,
            },
            workspace_id,
        )
