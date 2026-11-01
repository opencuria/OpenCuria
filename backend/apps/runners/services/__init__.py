"""Compose runner services and expose the adapter-facing application API.

Domain services own their behavior and mutable state. Internal callers use those
services directly; the facade exposes operations for REST, Socket.IO and harness.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser as User

    from ..models import (
        Runner,
        RunnerSystemMetrics,
        Task,
        Workspace,
        WorkspaceProcess,
    )
    from ..repositories import WorkspaceLaunchState


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
    """Adapter-facing facade over independently testable runner services."""

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

    def is_workspace_available(self, workspace_id: uuid.UUID) -> bool:
        """Delegate to :class:`OwnershipService`."""
        return self.ownership.is_workspace_available(workspace_id)

    def record_operation_heartbeat(self, runner_id: str, data: dict) -> None:
        """Delegate to :class:`RunnerTransportService`."""
        return self.runner_transport.record_operation_heartbeat(runner_id, data)

    async def emit_harness_event(
        self, runner: Runner, event: str, payload: dict
    ) -> None:
        """Delegate to :class:`RunnerTransportService`."""
        return await self.runner_transport.emit_harness_event(runner, event, payload)

    def handle_harness_reply(
        self, event: str, data: dict, runner_id: str | None = None
    ) -> bool:
        """Delegate to :class:`RunnerTransportService`."""
        return self.runner_transport.handle_harness_reply(event, data, runner_id)

    def is_desktop_active(self, workspace_id: str) -> bool:
        """Delegate to :class:`SessionStoreService`."""
        return self.session_store.is_desktop_active(workspace_id)

    def get_desktop_info(self, workspace_id: str) -> dict | None:
        """Delegate to :class:`SessionStoreService`."""
        return self.session_store.get_desktop_info(workspace_id)

    async def dispatch_credential_reconcile(
        self, workspace_ids: list[uuid.UUID]
    ) -> None:
        """Delegate to :class:`CredentialSyncService`."""
        return await self.credential_sync.dispatch_credential_reconcile(workspace_ids)

    def handle_credentials_injected(
        self,
        task_id: str,
        workspace_id: str,
        credentials_present: bool,
        runner_id: str | None = None,
    ) -> None:
        """Delegate to :class:`CredentialSyncService`."""
        return self.credential_sync.handle_credentials_injected(
            task_id, workspace_id, credentials_present, runner_id
        )

    async def editor_file_operation(
        self, workspace_id: uuid.UUID, operation: str, payload: dict
    ) -> dict:
        """Delegate to :class:`FileTransferService`."""
        return await self.file_transfer.editor_file_operation(
            workspace_id, operation, payload
        )

    async def forward_files_event(
        self, workspace_id: str, event: str, data: dict
    ) -> None:
        """Delegate to :class:`FileTransferService`."""
        return await self.file_transfer.forward_files_event(workspace_id, event, data)

    async def handle_files_result(
        self, event: str, data: dict, runner_id: str | None = None
    ) -> None:
        """Delegate to :class:`FileTransferService`."""
        return await self.file_transfer.handle_files_result(event, data, runner_id)

    def handle_git_reply(
        self, event: str, data: dict, runner_id: str | None = None
    ) -> bool:
        """Delegate to :class:`GitOperationsService`."""
        return self.git_operations.handle_git_reply(event, data, runner_id)

    async def run_git_operation(
        self,
        workspace_id: uuid.UUID,
        operation: str,
        *,
        repo_path: str | None = None,
        args: dict | None = None,
        user=None,
    ) -> dict:
        """Delegate to :class:`GitOperationsService`."""
        return await self.git_operations.run_git_operation(
            workspace_id, operation, repo_path=repo_path, args=args, user=user
        )

    async def auto_stop_inactive_workspaces(
        self,
        *,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        now: datetime | None = None,
        workspaces: list | None = None,
    ) -> list[Task]:
        """Delegate to :class:`HeartbeatReconcilerService`."""
        return await self.heartbeat_reconciler.auto_stop_inactive_workspaces(
            runner_id=runner_id,
            organization_id=organization_id,
            now=now,
            workspaces=workspaces,
        )

    async def auto_stop_if_inactive(
        self, workspace_id: uuid.UUID, *, now: datetime | None = None
    ) -> Task | None:
        """Delegate to :class:`HeartbeatReconcilerService`."""
        return await self.heartbeat_reconciler.auto_stop_if_inactive(
            workspace_id, now=now
        )

    def handle_heartbeat(
        self, runner: Runner, workspaces: list[dict]
    ) -> list[uuid.UUID]:
        """Delegate to :class:`HeartbeatReconcilerService`."""
        return self.heartbeat_reconciler.handle_heartbeat(runner, workspaces)

    async def reconcile_vanished_processes(self, runner: Runner) -> list[uuid.UUID]:
        """Delegate to :class:`HeartbeatReconcilerService`."""
        return await self.heartbeat_reconciler.reconcile_vanished_processes(runner)

    def deactivate_runner_image(self, build):
        """Delegate to :class:`ImageLifecycleService`."""
        return self.image_lifecycle.deactivate_runner_image(build)

    async def activate_build_job(self, build, *, created_by=None, message: str = ""):
        """Delegate to :class:`ImageLifecycleService`."""
        return await self.image_lifecycle.activate_build_job(
            build, created_by=created_by, message=message
        )

    async def trigger_build_job(
        self,
        *,
        image_definition,
        runner,
        activate: bool = True,
        created_by=None,
        message: str = "",
    ):
        """Delegate to :class:`ImageLifecycleService`."""
        return await self.image_lifecycle.trigger_build_job(
            image_definition=image_definition,
            runner=runner,
            activate=activate,
            created_by=created_by,
            message=message,
        )

    def handle_image_build_progress(
        self,
        build_job_id: str,
        line: str,
        runner_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Delegate to :class:`ImageLifecycleService`."""
        return self.image_lifecycle.handle_image_build_progress(
            build_job_id, line, runner_id, task_id
        )

    def handle_image_built(
        self,
        *,
        task_id: str,
        build_job_id: str,
        image_path: str = "",
        runner_id: str | None = None,
        min_disk_size_gb: int | None = None,
    ) -> None:
        """Delegate to :class:`ImageLifecycleService`."""
        return self.image_lifecycle.handle_image_built(
            task_id=task_id,
            build_job_id=build_job_id,
            image_path=image_path,
            runner_id=runner_id,
            min_disk_size_gb=min_disk_size_gb,
        )

    def handle_image_build_failed(
        self,
        *,
        task_id: str,
        build_job_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Delegate to :class:`ImageLifecycleService`."""
        return self.image_lifecycle.handle_image_build_failed(
            task_id=task_id, build_job_id=build_job_id, error=error, runner_id=runner_id
        )

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str = "",
        organization_id: uuid.UUID | None = None,
        captured_image_id: uuid.UUID | None = None,
        message: str = "",
    ) -> tuple[Workspace, Task]:
        """Delegate to :class:`ImageLifecycleService`."""
        return await self.image_lifecycle.create_image_artifact(
            workspace_id, name, organization_id, captured_image_id, message
        )

    def handle_image_artifact_created(
        self,
        task_id: str,
        workspace_id: str,
        artifact_id: str,
        name: str,
        size_bytes: int = 0,
        runner_id: str | None = None,
        min_disk_size_gb: int | None = None,
    ) -> None:
        """Delegate to :class:`ImageLifecycleService`."""
        return self.image_lifecycle.handle_image_artifact_created(
            task_id,
            workspace_id,
            artifact_id,
            name,
            size_bytes,
            runner_id,
            min_disk_size_gb,
        )

    def handle_image_artifact_failed(
        self,
        task_id: str,
        workspace_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Delegate to :class:`ImageLifecycleService`."""
        return self.image_lifecycle.handle_image_artifact_failed(
            task_id, workspace_id, error, runner_id
        )

    def handle_image_artifact_deleted(
        self,
        task_id: str,
        image_instance_id: str = "",
        runner_ref: str = "",
        result: str = "deleted",
        runner_id: str | None = None,
    ) -> None:
        """Delegate to :class:`ImageLifecycleService`."""
        return self.image_lifecycle.handle_image_artifact_deleted(
            task_id, image_instance_id, runner_ref, result, runner_id
        )

    def handle_image_artifact_delete_failed(
        self, task_id: str, error: str = "", runner_id: str | None = None
    ) -> None:
        """Delegate to :class:`ImageLifecycleService`."""
        return self.image_lifecycle.handle_image_artifact_delete_failed(
            task_id, error, runner_id
        )

    async def activate_image_definition(self, definition_id: uuid.UUID) -> None:
        """Delegate to :class:`ImageLifecycleService`."""
        return await self.image_lifecycle.activate_image_definition(definition_id)

    async def start_terminal(
        self, workspace_id: uuid.UUID, cols: int = 80, rows: int = 24
    ) -> Task:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.start_terminal(workspace_id, cols, rows)

    async def handle_terminal_started(
        self,
        task_id: str,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.handle_terminal_started(
            task_id, workspace_id, terminal_id, runner_id
        )

    async def handle_terminal_output(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
        runner_id: str | None = None,
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.handle_terminal_output(
            workspace_id, terminal_id, data, runner_id
        )

    async def handle_terminal_closed(
        self, workspace_id: str, terminal_id: str, runner_id: str | None = None
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.handle_terminal_closed(
            workspace_id, terminal_id, runner_id
        )

    async def forward_terminal_input(
        self, workspace_id: str, terminal_id: str, data: str
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.forward_terminal_input(
            workspace_id, terminal_id, data
        )

    async def forward_terminal_resize(
        self, workspace_id: str, terminal_id: str, cols: int, rows: int
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.forward_terminal_resize(
            workspace_id, terminal_id, cols, rows
        )

    async def forward_terminal_close(self, workspace_id: str, terminal_id: str) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.forward_terminal_close(
            workspace_id, terminal_id
        )

    @staticmethod
    def viewer_identity(
        workspace_id: uuid.UUID, user_id: uuid.UUID, viewer_client_id: uuid.UUID
    ) -> dict:
        """Delegate to :class:`InteractiveSessionsService`."""
        return InteractiveSessionsService.viewer_identity(
            workspace_id, user_id, viewer_client_id
        )

    async def take_desktop_control(
        self, workspace_id: uuid.UUID, *, user: User, organization_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.take_desktop_control(
            workspace_id, user=user, organization_id=organization_id
        )

    async def viewer_desktop_status(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
        viewer_client_id: uuid.UUID | None = None,
    ) -> dict:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.viewer_desktop_status(
            workspace_id,
            user=user,
            organization_id=organization_id,
            viewer_client_id=viewer_client_id,
        )

    def get_desktop_proxy_target(self, workspace_id: str) -> dict | None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return self.interactive_sessions.get_desktop_proxy_target(workspace_id)

    async def renew_desktop(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
        viewer_client_id: uuid.UUID,
        intent_revision: int,
    ) -> dict:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.renew_desktop(
            workspace_id,
            user=user,
            organization_id=organization_id,
            viewer_client_id=viewer_client_id,
            intent_revision=intent_revision,
        )

    async def start_desktop(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
        viewer_client_id: uuid.UUID,
        intent_revision: int,
    ) -> Task:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.start_desktop(
            workspace_id,
            user=user,
            organization_id=organization_id,
            viewer_client_id=viewer_client_id,
            intent_revision=intent_revision,
        )

    async def stop_desktop(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
        viewer_client_id: uuid.UUID,
        intent_revision: int,
    ) -> Task:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.stop_desktop(
            workspace_id,
            user=user,
            organization_id=organization_id,
            viewer_client_id=viewer_client_id,
            intent_revision=intent_revision,
        )

    async def write_desktop_clipboard(
        self,
        workspace_id: uuid.UUID,
        text: str,
        *,
        user: User,
        organization_id: uuid.UUID,
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.write_desktop_clipboard(
            workspace_id, text, user=user, organization_id=organization_id
        )

    async def read_desktop_clipboard(
        self,
        workspace_id: uuid.UUID,
        *,
        user: User,
        organization_id: uuid.UUID,
        copy_selection: bool = False,
        shift: bool = False,
    ) -> str:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.read_desktop_clipboard(
            workspace_id,
            user=user,
            organization_id=organization_id,
            copy_selection=copy_selection,
            shift=shift,
        )

    async def handle_desktop_started(
        self,
        task_id: str | None,
        workspace_id: str,
        port: int,
        guest_ip: str,
        runner_id: str,
        *,
        viewer: bool,
        computer_use: bool,
        mcp: bool,
        holder_count: int,
        generation: int,
        epoch: str,
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.handle_desktop_started(
            task_id,
            workspace_id,
            port,
            guest_ip,
            runner_id,
            viewer=viewer,
            computer_use=computer_use,
            mcp=mcp,
            holder_count=holder_count,
            generation=generation,
            epoch=epoch,
        )

    async def handle_desktop_process(
        self,
        workspace_id: str,
        port: int,
        guest_ip: str,
        runner_id: str,
        *,
        viewer: bool,
        computer_use: bool,
        mcp: bool,
        holder_count: int,
        generation: int,
        epoch: str,
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.handle_desktop_process(
            workspace_id,
            port,
            guest_ip,
            runner_id,
            viewer=viewer,
            computer_use=computer_use,
            mcp=mcp,
            holder_count=holder_count,
            generation=generation,
            epoch=epoch,
        )

    async def handle_desktop_viewer_released(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str,
        *,
        computer_use_active: bool,
        viewer_held: bool,
        mcp: bool,
        holder_count: int,
        generation: int,
        epoch: str,
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.handle_desktop_viewer_released(
            task_id,
            workspace_id,
            runner_id,
            computer_use_active=computer_use_active,
            viewer_held=viewer_held,
            mcp=mcp,
            holder_count=holder_count,
            generation=generation,
            epoch=epoch,
        )

    async def handle_desktop_stopped(
        self,
        task_id: str | None,
        workspace_id: str,
        runner_id: str,
        *,
        generation: int,
        epoch: str,
    ) -> None:
        """Delegate to :class:`InteractiveSessionsService`."""
        return await self.interactive_sessions.handle_desktop_stopped(
            task_id, workspace_id, runner_id, generation=generation, epoch=epoch
        )

    def handle_process_reply(
        self, event: str, data: dict, runner_id: str | None = None
    ) -> bool:
        """Delegate to :class:`ProcessManagerService`."""
        return self.process_manager.handle_process_reply(event, data, runner_id)

    async def start_process(
        self,
        workspace_id: uuid.UUID,
        command: str,
        *,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        name: str,
        user=None,
        session_id: uuid.UUID | str | None = None,
        kind: str | None = None,
    ) -> WorkspaceProcess:
        """Delegate to :class:`ProcessManagerService`."""
        return await self.process_manager.start_process(
            workspace_id,
            command,
            workdir=workdir,
            env=env,
            name=name,
            user=user,
            session_id=session_id,
            kind=kind,
        )

    async def reverify_vanished_processes(
        self, workspace_id: uuid.UUID, vanished: list[WorkspaceProcess]
    ) -> list[uuid.UUID]:
        """Delegate to :class:`ProcessManagerService`."""
        return await self.process_manager.reverify_vanished_processes(
            workspace_id, vanished
        )

    def sweep_unconfirmed_processes(
        self, workspace_id: str, *, max_age_seconds: float | None = None
    ) -> list[uuid.UUID]:
        """Delegate to :class:`ProcessManagerService`."""
        return self.process_manager.sweep_unconfirmed_processes(
            workspace_id, max_age_seconds=max_age_seconds
        )

    async def restart_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        user=None,
        session_id: uuid.UUID | str | None = None,
    ) -> WorkspaceProcess:
        """Delegate to :class:`ProcessManagerService`."""
        return await self.process_manager.restart_process(
            workspace_id, id_or_name, user=user, session_id=session_id
        )

    async def delete_process(
        self, workspace_id: uuid.UUID, id_or_name: uuid.UUID | str
    ) -> uuid.UUID:
        """Delegate to :class:`ProcessManagerService`."""
        return await self.process_manager.delete_process(workspace_id, id_or_name)

    async def list_processes(
        self,
        workspace_id: uuid.UUID,
        *,
        kinds: tuple[str, ...] | None = None,
        running_only: bool = False,
        session_id: uuid.UUID | str | None = None,
        live: bool = True,
    ) -> list[WorkspaceProcess]:
        """Delegate to :class:`ProcessManagerService`."""
        return await self.process_manager.list_processes(
            workspace_id,
            kinds=kinds,
            running_only=running_only,
            session_id=session_id,
            live=live,
        )

    async def get_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        session_id: uuid.UUID | str | None = None,
    ) -> WorkspaceProcess:
        """Delegate to :class:`ProcessManagerService`."""
        return await self.process_manager.get_process(
            workspace_id, id_or_name, session_id=session_id
        )

    async def stop_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        session_id: uuid.UUID | str | None = None,
    ) -> WorkspaceProcess:
        """Delegate to :class:`ProcessManagerService`."""
        return await self.process_manager.stop_process(
            workspace_id, id_or_name, session_id=session_id
        )

    async def stop_session_processes(
        self,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID | str,
        *,
        kind: str = "temp",
        reason: str = "session_finished",
    ) -> list[WorkspaceProcess]:
        """Delegate to :class:`ProcessManagerService`."""
        return await self.process_manager.stop_session_processes(
            workspace_id, session_id, kind=kind, reason=reason
        )

    def reconcile_workspace_processes(
        self, workspace_id: str, reported: list[dict]
    ) -> tuple[list[uuid.UUID], list[WorkspaceProcess]]:
        """Delegate to :class:`ProcessManagerService`."""
        return self.process_manager.reconcile_workspace_processes(
            workspace_id, reported
        )

    def mark_processes_killed(
        self, workspace_id: str, *, reason: str = "workspace_stopped"
    ) -> int:
        """Delegate to :class:`ProcessManagerService`."""
        return self.process_manager.mark_processes_killed(workspace_id, reason=reason)

    def create_runner(self, *, name: str, organization) -> tuple[Runner, str]:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.create_runner(name=name, organization=organization)

    def authenticate_runner(self, token: str) -> Runner:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.authenticate_runner(token)

    def register_runner(
        self, runner: Runner, *, sid: str, desktop_epoch: str | None = None
    ) -> Runner:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.register_runner(
            runner, sid=sid, desktop_epoch=desktop_epoch
        )

    def unregister_runner(self, sid: str) -> str | None:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.unregister_runner(sid)

    def is_current_runner_session(
        self, runner_id: str, sid: str, *, require_online: bool = True
    ) -> bool:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.is_current_runner_session(
            runner_id, sid, require_online=require_online
        )

    def record_runner_heartbeat(self, runner: Runner) -> Runner:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.record_runner_heartbeat(runner)

    def record_runner_system_metrics(self, runner: Runner, metrics: dict) -> None:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.record_runner_system_metrics(runner, metrics)

    def get_latest_runner_metrics(
        self, runner_id: uuid.UUID
    ) -> RunnerSystemMetrics | None:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.get_latest_runner_metrics(runner_id)

    def list_runner_metrics(
        self, runner_id: uuid.UUID, *, hours: int
    ) -> list[RunnerSystemMetrics]:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.list_runner_metrics(runner_id, hours=hours)

    def update_runner_qemu_settings(self, runner_id: uuid.UUID, **fields) -> Runner:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.update_runner_qemu_settings(runner_id, **fields)

    def list_runners(self, organization_id: uuid.UUID | None = None) -> list[Runner]:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.list_runners(organization_id)

    def get_runner(self, runner_id: uuid.UUID) -> Runner:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.get_runner(runner_id)

    def get_runner_or_none(self, runner_id: uuid.UUID) -> Runner | None:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.get_runner_or_none(runner_id)

    def list_workspaces(
        self,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        user=None,
    ) -> list[Workspace]:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.list_workspaces(runner_id, organization_id, user)

    def get_workspace(self, workspace_id: uuid.UUID) -> Workspace:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.get_workspace(workspace_id)

    def get_workspace_or_none(self, workspace_id: uuid.UUID) -> Workspace | None:
        """Delegate to :class:`RunnerLifecycleService`."""
        return self.runner_lifecycle.get_workspace_or_none(workspace_id)

    def handle_stream_reply(
        self, event: str, data: dict, runner_id: str | None = None
    ) -> bool:
        """Delegate to :class:`StreamTransportService`."""
        return self.stream_transport.handle_stream_reply(event, data, runner_id)

    async def call_stream_event(
        self, runner: Runner, event: str, payload: dict, timeout: float | None = None
    ) -> dict:
        """Delegate to :class:`StreamTransportService`."""
        return await self.stream_transport.call_stream_event(
            runner, event, payload, timeout
        )

    def fail_streams_for_runner(self, runner_id: str) -> None:
        """Delegate to :class:`StreamTransportService`."""
        return self.stream_transport.fail_streams_for_runner(runner_id)

    def get_workspace_for_user(
        self, workspace_id: uuid.UUID, *, user, organization_id: uuid.UUID
    ) -> Workspace:
        """Delegate to :class:`WorkspaceAccessService`."""
        return self.workspace_access.get_workspace_for_user(
            workspace_id, user=user, organization_id=organization_id
        )

    def can_user_access_workspace(self, user_id: int | str, workspace_id: str) -> bool:
        """Delegate to :class:`WorkspaceAccessService`."""
        return self.workspace_access.can_user_access_workspace(user_id, workspace_id)

    def is_active_workspace_owner(
        self, user_id: int, workspace_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> bool:
        """Delegate to :class:`WorkspaceAccessService`."""
        return self.workspace_access.is_active_workspace_owner(
            user_id, workspace_id, organization_id=organization_id
        )

    def can_user_view_workspace_desktop(
        self, user_id: int | str, workspace_id: str
    ) -> bool:
        """Delegate to :class:`WorkspaceAccessService`."""
        return self.workspace_access.can_user_view_workspace_desktop(
            user_id, workspace_id
        )

    def get_workspace_for_desktop_user(
        self, workspace_id: uuid.UUID, *, user: User, organization_id: uuid.UUID
    ) -> Workspace:
        """Delegate to :class:`WorkspaceAccessService`."""
        return self.workspace_access.get_workspace_for_desktop_user(
            workspace_id, user=user, organization_id=organization_id
        )

    def resolve_desktop_viewer(
        self, user_id: int | str, workspace_id: str
    ) -> tuple[User, uuid.UUID]:
        """Delegate to :class:`WorkspaceAccessService`."""
        return self.workspace_access.resolve_desktop_viewer(user_id, workspace_id)

    async def touch_workspace_activity(
        self, workspace_id: uuid.UUID, *, at: datetime | None = None
    ) -> None:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.touch_workspace_activity(
            workspace_id, at=at
        )

    def get_workspace_launch_state(
        self, workspace_id: uuid.UUID
    ) -> WorkspaceLaunchState:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return self.workspace_lifecycle.get_workspace_launch_state(workspace_id)

    async def create_workspace(
        self,
        *,
        name: str,
        repos: list[str],
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        desktop_width: int | None = None,
        desktop_height: int | None = None,
        credential_ids: list[uuid.UUID] | None = None,
        runner_id: uuid.UUID | None = None,
        image_artifact_id: uuid.UUID | None = None,
        user=None,
        organization_id: uuid.UUID | None = None,
        plugin_ids: list[uuid.UUID] | None = None,
    ) -> tuple[Workspace, Task]:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.create_workspace(
            name=name,
            repos=repos,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            desktop_width=desktop_width,
            desktop_height=desktop_height,
            credential_ids=credential_ids,
            runner_id=runner_id,
            image_artifact_id=image_artifact_id,
            user=user,
            organization_id=organization_id,
            plugin_ids=plugin_ids,
        )

    async def update_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        name: str | None = None,
        credential_ids: list[uuid.UUID] | None = None,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        desktop_width: int | None = None,
        desktop_height: int | None = None,
        plugin_ids: list[uuid.UUID] | None = None,
        user=None,
        organization_id: uuid.UUID | None = None,
    ) -> Workspace:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.update_workspace(
            workspace_id,
            name=name,
            credential_ids=credential_ids,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            desktop_width=desktop_width,
            desktop_height=desktop_height,
            plugin_ids=plugin_ids,
            user=user,
            organization_id=organization_id,
        )

    async def rename_workspace(self, workspace_id: uuid.UUID, name: str) -> Workspace:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.rename_workspace(workspace_id, name)

    async def stop_workspace(
        self, workspace_id: uuid.UUID, *, auto_stop: bool = False
    ) -> Task:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.stop_workspace(
            workspace_id, auto_stop=auto_stop
        )

    async def resume_workspace(self, workspace_id: uuid.UUID) -> Task:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.resume_workspace(workspace_id)

    async def recreate_workspace(
        self,
        workspace_id: uuid.UUID,
        image_id: uuid.UUID,
        *,
        user=None,
        organization_id: uuid.UUID | None = None,
    ) -> tuple[Workspace, Task]:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.recreate_workspace(
            workspace_id, image_id, user=user, organization_id=organization_id
        )

    async def remove_workspace(self, workspace_id: uuid.UUID) -> Task:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.remove_workspace(workspace_id)

    def handle_workspace_created(
        self,
        task_id: str,
        workspace_id: str,
        status: str,
        credentials_present: bool | None = None,
        runner_id: str | None = None,
    ) -> None:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return self.workspace_lifecycle.handle_workspace_created(
            task_id, workspace_id, status, credentials_present, runner_id
        )

    def handle_workspace_stopped(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
        credentials_present: bool | None = None,
    ) -> None:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return self.workspace_lifecycle.handle_workspace_stopped(
            task_id, workspace_id, runner_id, credentials_present
        )

    def handle_workspace_resumed(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
        credentials_present: bool | None = None,
    ) -> None:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return self.workspace_lifecycle.handle_workspace_resumed(
            task_id, workspace_id, runner_id, credentials_present
        )

    def handle_workspace_updated(
        self, task_id: str, workspace_id: str, runner_id: str | None = None
    ) -> None:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return self.workspace_lifecycle.handle_workspace_updated(
            task_id, workspace_id, runner_id
        )

    def handle_workspace_rpc_error(
        self, task_id: str, error: str, runner_id: str | None = None
    ) -> None:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return self.workspace_lifecycle.handle_workspace_rpc_error(
            task_id, error, runner_id
        )

    def handle_workspace_error(
        self,
        task_id: str,
        error: str,
        runner_id: str | None = None,
        observed_status: str | None = None,
        credentials_present: bool | None = None,
    ) -> None:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return self.workspace_lifecycle.handle_workspace_error(
            task_id, error, runner_id, observed_status, credentials_present
        )

    def handle_workspace_removed(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
        result: str = "deleted",
        already_absent: bool = False,
    ) -> None:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return self.workspace_lifecycle.handle_workspace_removed(
            task_id, workspace_id, runner_id, result, already_absent
        )

    async def create_workspace_from_image_artifact(
        self,
        image_artifact_id: uuid.UUID,
        name: str = "",
        credential_ids: list[uuid.UUID] | None = None,
        user=None,
        organization_id: uuid.UUID | None = None,
        plugin_ids: list[uuid.UUID] | None = None,
    ) -> tuple[Workspace, Task]:
        """Delegate to :class:`WorkspaceLifecycleService`."""
        return await self.workspace_lifecycle.create_workspace_from_image_artifact(
            image_artifact_id, name, credential_ids, user, organization_id, plugin_ids
        )


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
