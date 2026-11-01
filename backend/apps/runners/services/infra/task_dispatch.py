"""Publish and deliver reserved workspace tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from ...enums import WorkspaceOperation

if TYPE_CHECKING:
    from ...models import Runner, Task, Workspace

if TYPE_CHECKING:
    from ...repositories import TaskRepository, WorkspaceRepository
    from ..infra.frontend_bus import FrontendBusService
    from ..ports import RunnerTransport

class TaskDispatchService:
    """Workspace task dispatch helpers with explicitly supplied collaborators."""

    def __init__(
        self,
        *,
        tasks: type[TaskRepository],
        workspaces: type[WorkspaceRepository],
        frontend_bus: FrontendBusService,
        runner_transport: RunnerTransport,
    ) -> None:
        """Compose this service without inheriting sibling behavior or state."""
        self.tasks = tasks
        self.workspaces = workspaces
        self.frontend_bus = frontend_bus
        self.runner_transport = runner_transport

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
        self.frontend_bus.forward_workspace_operation(
            str(workspace.id), workspace.active_operation
        )
        return workspace

    async def dispatch_workspace_task(
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
            self.frontend_bus.forward_workspace_operation(str(workspace.id), operation)
        await self.runner_transport.emit_to_runner(runner, event, payload)
        await sync_to_async(self.tasks.mark_in_progress)(task)
