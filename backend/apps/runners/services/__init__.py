"""Compose the runner domain services for one process.

Domain services own their behavior and mutable state. Adapters (REST,
Socket.IO, MCP, harness) call the owning service directly, e.g.
``get_runner_service().workspace_lifecycle.stop_workspace(...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.realtime import get_sio_server

from ..process_repository import WorkspaceProcessRepository
from ..repositories import (
    ImageBuildJobRepository,
    ImageDefinitionRepository,
    ImageInstanceRepository,
    RunnerRepository,
    RunnerSystemMetricsRepository,
    TaskRepository,
    WorkspaceRepository,
)
from .domains.credential_sync import CredentialSyncService
from .domains.file_transfer import FileTransferService
from .domains.git_operations import GitOperationsService
from .domains.heartbeat_reconciler import HeartbeatReconcilerService
from .domains.image_lifecycle import ImageLifecycleService
from .domains.interactive_sessions import InteractiveSessionsService
from .domains.process_manager import ProcessManagerService
from .domains.runner_lifecycle import RunnerLifecycleService
from .domains.stream_transport import StreamTransportService
from .domains.workspace_access import WorkspaceAccessService
from .domains.workspace_lifecycle import WorkspaceLifecycleService
from .infra.frontend_bus import FrontendBusService
from .infra.harness_gateway import get_harness_gateway
from .infra.ownership import OwnershipService
from .infra.rpc_registry import RpcRegistryService
from .infra.runner_transport import RunnerTransportService
from .infra.session_store import SessionStoreService
from .infra.task_dispatch import TaskDispatchService
from .operation_results import apply_result
from .ports import CredentialResolver, HarnessGateway
from .session_state import InteractiveSessionState


@dataclass(frozen=True)
class RunnerRepositories:
    """Persistence adapters selected once at composition time."""

    runners: type[RunnerRepository] = RunnerRepository
    runner_metrics: type[RunnerSystemMetricsRepository] = RunnerSystemMetricsRepository
    workspaces: type[WorkspaceRepository] = WorkspaceRepository
    tasks: type[TaskRepository] = TaskRepository
    image_instances: type[ImageInstanceRepository] = ImageInstanceRepository
    image_definitions: type[ImageDefinitionRepository] = ImageDefinitionRepository
    build_jobs: type[ImageBuildJobRepository] = ImageBuildJobRepository
    processes: type[WorkspaceProcessRepository] = WorkspaceProcessRepository


class RunnerService:
    """Composition root wiring the independently testable runner services."""

    def __init__(
        self,
        sio_server: Any = None,
        *,
        credential_resolver: CredentialResolver | None = None,
        harness_gateway: HarnessGateway | None = None,
        repositories: RunnerRepositories | None = None,
    ) -> None:
        """Wire explicit service dependencies without shared mutable class state."""
        repositories = repositories or RunnerRepositories()
        self._harness_gateway = harness_gateway
        state = InteractiveSessionState()
        self.runners = repositories.runners
        self.runner_metrics = repositories.runner_metrics
        self.workspaces = repositories.workspaces
        self.tasks = repositories.tasks
        self.image_instances = repositories.image_instances
        self.image_definitions = repositories.image_definitions
        self.build_jobs = repositories.build_jobs
        self.processes = repositories.processes

        self.frontend_bus = FrontendBusService(
            workspaces=repositories.workspaces,
        )
        self.ownership = OwnershipService(
            workspaces=repositories.workspaces,
            state=state,
            operation_label=WorkspaceLifecycleService.workspace_operation_label,
        )
        self.rpc_registry = RpcRegistryService()
        self.runner_transport = RunnerTransportService(
            runners=repositories.runners,
            harness=self._harness,
            ownership=self.ownership,
            validate_file_chunk=FileTransferService.validate_runner_file_chunk,
            sio_server=sio_server,
        )
        self.session_store = SessionStoreService(
            state=state,
        )
        self.task_dispatch = TaskDispatchService(
            tasks=repositories.tasks,
            workspaces=repositories.workspaces,
            frontend_bus=self.frontend_bus,
            runner_transport=self.runner_transport,
        )
        self.credential_sync = CredentialSyncService(
            tasks=repositories.tasks,
            workspaces=repositories.workspaces,
            apply_operation_result=self.apply_operation_result,
            frontend_bus=self.frontend_bus,
            ownership=self.ownership,
            runner_transport=self.runner_transport,
            credential_resolver=credential_resolver,
        )
        self.image_lifecycle = ImageLifecycleService(
            build_jobs=repositories.build_jobs,
            image_definitions=repositories.image_definitions,
            image_instances=repositories.image_instances,
            tasks=repositories.tasks,
            workspaces=repositories.workspaces,
            frontend_bus=self.frontend_bus,
            harness=self._harness,
            runner_transport=self.runner_transport,
        )
        self.process_manager = ProcessManagerService(
            processes=repositories.processes,
            workspaces=repositories.workspaces,
            frontend_bus=self.frontend_bus,
            harness=self._harness,
            ownership=self.ownership,
            rpc_registry=self.rpc_registry,
            runner_transport=self.runner_transport,
        )
        self.stream_transport = StreamTransportService(
            workspaces=repositories.workspaces,
            harness=self._harness,
            ownership=self.ownership,
            runner_transport=self.runner_transport,
        )
        self.workspace_lifecycle = WorkspaceLifecycleService(
            image_instances=repositories.image_instances,
            tasks=repositories.tasks,
            workspaces=repositories.workspaces,
            credential_sync=self.credential_sync,
            frontend_bus=self.frontend_bus,
            harness=self._harness,
            ownership=self.ownership,
            process_manager=self.process_manager,
            runner_transport=self.runner_transport,
            session_store=self.session_store,
            task_dispatch=self.task_dispatch,
        )
        self.file_transfer = FileTransferService(
            workspaces=repositories.workspaces,
            ownership=self.ownership,
            runner_transport=self.runner_transport,
            workspace_lifecycle=self.workspace_lifecycle,
        )
        self.git_operations = GitOperationsService(
            workspaces=repositories.workspaces,
            ownership=self.ownership,
            rpc_registry=self.rpc_registry,
            runner_transport=self.runner_transport,
            workspace_lifecycle=self.workspace_lifecycle,
        )
        self.runner_lifecycle = RunnerLifecycleService(
            runner_metrics=repositories.runner_metrics,
            runners=repositories.runners,
            workspaces=repositories.workspaces,
            frontend_bus=self.frontend_bus,
            harness=self._harness,
            session_store=self.session_store,
            stream_transport=self.stream_transport,
        )
        self.workspace_access = WorkspaceAccessService(
            workspaces=repositories.workspaces,
            runner_lifecycle=self.runner_lifecycle,
        )
        self.heartbeat_reconciler = HeartbeatReconcilerService(
            runners=repositories.runners,
            workspaces=repositories.workspaces,
            frontend_bus=self.frontend_bus,
            process_manager=self.process_manager,
            runner_lifecycle=self.runner_lifecycle,
            session_store=self.session_store,
            workspace_lifecycle=self.workspace_lifecycle,
        )
        self.interactive_sessions = InteractiveSessionsService(
            tasks=repositories.tasks,
            workspaces=repositories.workspaces,
            state=state,
            desktop_action=self._desktop_action,
            harness=self._harness,
            ownership=self.ownership,
            runner_transport=self.runner_transport,
            session_store=self.session_store,
            workspace_access=self.workspace_access,
        )

    def _harness(self) -> HarnessGateway:
        """Resolve the injected or registered harness integration."""
        return self._harness_gateway or get_harness_gateway()

    async def _desktop_action(self, workspace_id: str, action: str, args: dict) -> dict:
        """Adapt the harness desktop integration at the composition boundary."""
        return await self._harness().desktop_action(self, workspace_id, action, args)

    def apply_operation_result(self, runner_id: str, event: str, data: dict) -> bool:
        """Coordinate journal-result validation and domain result handlers."""
        return apply_result(self, runner_id, event, data)


_runner_service: RunnerService | None = None


def get_runner_service() -> RunnerService:
    """Return the application composition root for the current process."""
    global _runner_service
    if _runner_service is None:
        _runner_service = RunnerService(sio_server=get_sio_server())
    return _runner_service


__all__ = [
    "RunnerService",
    "RunnerRepositories",
    "get_runner_service",
]
