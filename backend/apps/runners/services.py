"""
Service layer for the runners app.

All business logic lives here. API views and WebSocket consumers delegate
to these functions — they never contain business logic themselves.

The service layer uses repositories for data access and the Socket.IO
server instance for sending events to runners.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import uuid
from datetime import datetime, timedelta

from asgiref.sync import async_to_sync, sync_to_async
from django.utils import timezone

from apps.credentials.services import CredentialSvc
from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.utils import generate_uuid, hash_token, verify_token

from .enums import (
    AgentCommandPhase,
    RunnerStatus,
    RuntimeType,
    SessionStatus,
    TaskStatus,
    TaskType,
    WorkspaceOperation,
    WorkspaceStatus,
)
from .exceptions import (
    NoAvailableRunnerError,
    RunnerNotFoundError,
    RunnerOfflineError,
    TaskNotFoundError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)
from .repositories import (
    AgentRepository,
    ChatRepository,
    ImageArtifactRepository,
    ImageDefinitionRepository,
    RunnerRepository,
    RunnerImageBuildRepository,
    SessionRepository,
    TaskRepository,
    WorkspaceRepository,
)

logger = logging.getLogger(__name__)
USER_CANCELLED_PROMPT_ERROR = "Prompt execution cancelled by user"


class RunnerService:
    """
    Central business logic for runner management and task dispatching.

    This is the only place where domain rules are enforced. All interfaces
    (REST API, Socket.IO consumers) delegate to this service.
    """

    def __init__(self, sio_server=None):
        """
        Initialize the service.

        Args:
            sio_server: The python-socketio AsyncServer instance for sending
                        events to connected runners. Injected to keep the
                        service testable.
        """
        self.sio = sio_server
        self.runners = RunnerRepository
        self.workspaces = WorkspaceRepository
        self.sessions = SessionRepository
        self.tasks = TaskRepository
        self.agents = AgentRepository
        self.chats = ChatRepository
        self.image_artifacts = ImageArtifactRepository
        self.image_definitions = ImageDefinitionRepository
        self.runner_image_builds = RunnerImageBuildRepository
        # Tracks unknown runtime workspaces for which a cleanup request has
        # already been sent, to avoid emitting duplicate cleanup tasks on every
        # heartbeat while one is still in flight.
        self._pending_unknown_workspace_cleanup: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Runner lifecycle
    # ------------------------------------------------------------------

    def authenticate_runner(self, token: str) -> "Runner":
        """
        Authenticate a runner by its API token.

        Returns the Runner instance if valid, raises AuthenticationError otherwise.
        """
        token_hash = hash_token(token)
        runner = self.runners.get_by_token_hash(token_hash)
        if runner is None:
            raise AuthenticationError("Invalid runner API token")
        return runner

    def register_runner(
        self,
        runner: "Runner",
        *,
        sid: str,
        available_runtimes: list[str] | None = None,
    ) -> "Runner":
        """
        Mark a runner as online after it connects and sends runner:register.

        Args:
            runner: The authenticated Runner instance.
            sid: Socket.IO session ID for targeted messaging.
            available_runtimes: List of runtime types the runner supports.
        """
        runner = self.runners.set_online(
            runner,
            sid=sid,
            available_runtimes=available_runtimes or ["docker"],
        )
        logger.info(
            "Runner registered: %s",
            runner.id,
        )
        return runner

    async def dispatch_pending_image_builds(self, runner: "Runner") -> list:
        """Dispatch pending image builds that were created while the runner was offline.

        This is called after a runner registers online.  It queries for
        ``RunnerImageBuild`` records with status ``pending`` and no associated
        build task, then triggers the regular build pipeline for each.

        Returns the list of dispatched RunnerImageBuild records.
        """
        from .models import RunnerImageBuild

        pending_builds = await sync_to_async(
            lambda: list(
                RunnerImageBuild.objects.filter(
                    runner=runner,
                    status=RunnerImageBuild.Status.PENDING,
                    build_task__isnull=True,
                ).select_related("image_definition", "runner")
            )
        )()

        dispatched = []
        for build in pending_builds:
            try:
                await self.trigger_runner_image_build(
                    image_definition=build.image_definition,
                    runner=runner,
                    activate=True,
                )
                dispatched.append(build)
                logger.info(
                    "Dispatched pending image build %s for runner %s",
                    build.id,
                    runner.id,
                )
            except Exception:
                logger.exception(
                    "Failed to dispatch pending image build %s for runner %s",
                    build.id,
                    runner.id,
                )
        return dispatched

    def unregister_runner(self, sid: str) -> None:
        """
        Mark a runner as offline when it disconnects.

        Looks up the runner by its Socket.IO session ID. Any sessions that are
        still active (PENDING / RUNNING) on this runner's workspaces are
        immediately marked as FAILED so the frontend does not get stuck waiting
        for output that will never arrive.
        """
        from .models import Runner, Session, Task

        try:
            runner = Runner.objects.get(sid=sid, status=RunnerStatus.ONLINE)
        except Runner.DoesNotExist:
            logger.warning("Disconnect from unknown SID: %s", sid)
            return

        # Collect active sessions BEFORE marking runner offline so we can
        # still resolve them via the FK chain.
        active_sessions = list(
            Session.objects.filter(
                chat__workspace__runner=runner,
                status__in=[SessionStatus.PENDING, SessionStatus.RUNNING],
            ).select_related("chat__workspace")
        )

        # Also fail any pending/in-progress tasks on this runner's workspaces
        # that are for run_prompt (so task state is consistent).
        from .enums import TaskStatus as TS
        Task.objects.filter(
            runner=runner,
            status__in=[TaskStatus.PENDING, TaskStatus.IN_PROGRESS],
            type=TaskType.RUN_PROMPT,
        ).update(
            status=TaskStatus.FAILED,
            error="Runner went offline",
        )

        self.runners.set_offline(runner)
        logger.info("Runner unregistered: %s", runner.id)

        # Notify frontend about runner going offline so it can update display.
        self._forward_runner_status_to_frontend(runner, "offline")

        # Fail stuck sessions and notify frontend.
        for session in active_sessions:
            self.sessions.fail(session, error_message="Runner went offline")
            workspace_id = str(session.chat.workspace_id)
            session_id = str(session.id)
            chat_id = str(session.chat_id) if session.chat_id else None
            logger.warning(
                "Failing session %s because runner %s went offline",
                session_id,
                runner.id,
            )
            self._forward_to_frontend(
                "session:failed",
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "error": "Runner went offline",
                },
                workspace_id,
            )

    def _forward_runner_status_to_frontend(
        self,
        runner: "Runner",
        status: str,
    ) -> None:
        """Emit runner status change events for all workspaces of this runner.

        Sends a ``runner:offline`` or ``runner:online`` event for every
        workspace managed by the runner so subscribed frontend clients can
        update their display without a full page refresh.
        """
        workspaces = list(self.workspaces.list_by_runner(runner.id).exclude(
            status__in=[WorkspaceStatus.REMOVED, WorkspaceStatus.FAILED]
        ))
        event = "runner:offline" if status == "offline" else "runner:online"
        for ws in workspaces:
            ws_id = str(ws.id)
            self._forward_to_frontend(
                event,
                {"workspace_id": ws_id, "runner_id": str(runner.id)},
                ws_id,
            )

    async def touch_workspace_activity(
        self,
        workspace_id: uuid.UUID,
        *,
        at: datetime | None = None,
    ) -> None:
        """Update the workspace last-activity timestamp."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            return
        await sync_to_async(self.workspaces.touch_activity)(workspace, at=at)

    # ------------------------------------------------------------------
    # Agent command helpers
    # ------------------------------------------------------------------

    def get_configure_commands(self, agent_definition_id: uuid.UUID) -> list[dict]:
        """Load configure-phase commands from the DB for an agent.

        Returns a list of structured command dicts ready for dispatch.
        """
        commands = self.agents.get_configure_commands(agent_definition_id)
        return [
            {
                "args": cmd.args,
                "workdir": cmd.workdir,
                "env": cmd.env or {},
                "description": cmd.description,
            }
            for cmd in commands
        ]

    @staticmethod
    def _get_agent_required_credential_service_slugs(agent_def) -> list[str]:
        """Return the credential service slugs required by an agent."""
        return [
            svc.slug
            for svc in agent_def.required_credential_services.all()
            if svc.slug
        ]

    @staticmethod
    def _get_workspace_credential_service_slugs(workspace) -> set[str]:
        """Return the credential service slugs available inside a workspace."""
        return {
            credential.service.slug
            for credential in workspace.credentials.all()
            if credential.service_id and credential.service.slug
        }

    @staticmethod
    def _merge_workspace_env_vars(
        command: dict,
        workspace_env_vars: dict[str, str],
    ) -> dict:
        """Return a command dict with workspace env vars merged in."""
        return {
            **command,
            "env": {
                **workspace_env_vars,
                **(command.get("env") or {}),
            },
        }

    def build_run_command(
        self,
        agent_definition_id: uuid.UUID,
        prompt: str,
        model: str = "",
        workdir: str = "/workspace",
        chat_id: str = "",
        is_first_message: bool = False,
        extra_options: dict[str, str] | None = None,
        additional_default_env: dict[str, str] | None = None,
    ) -> dict:
        """Build a run command dict by rendering the DB template.

        Substitutes ``{prompt}``, ``{workdir}``, ``{model}``, ``{chat_id}``
        and any additional ``{key}`` placeholders from ``extra_options`` in
        the stored command template.

        When ``is_first_message`` is True and the agent has a ``run_first``
        command defined, that template is used instead of ``run``. This
        supports agents like Claude Code that require a different invocation
        (e.g. ``--session-id``) for the very first message in a chat.

        Returns a structured command dict ready for dispatch to a runner.
        """
        cmd = None
        if is_first_message:
            cmd = self.agents.get_run_first_command(agent_definition_id)
        if cmd is None:
            cmd = self.agents.get_run_command(agent_definition_id)
        if cmd is None:
            raise ValueError(
                f"No run command defined for agent '{agent_definition_id}'"
            )

        # Fetch agent default_env; per-command env takes precedence.
        agent_def = self.agents.get_by_id(agent_definition_id)
        default_env: dict[str, str] = (
            agent_def.default_env if agent_def and agent_def.default_env else {}
        )
        merged_env = {
            **(additional_default_env or {}),
            **default_env,
            **(cmd.env or {}),
        }

        options = extra_options or {}

        def _render(arg: str) -> str:
            result = (
                arg.replace("{prompt}", prompt)
                .replace("{workdir}", workdir)
                .replace("{model}", model)
                .replace("{chat_id}", chat_id)
            )
            for key, value in options.items():
                result = result.replace(f"{{{key}}}", value)
            return result

        rendered_args = [_render(arg) for arg in cmd.args]
        rendered_workdir = (
            _render(cmd.workdir)
            if cmd.workdir
            else workdir
        )

        return {
            "args": rendered_args,
            "workdir": rendered_workdir,
            "env": merged_env,
            "description": cmd.description,
        }

    def _build_render_context(
        self,
        *,
        prompt: str,
        model: str,
        workdir: str,
        chat_id: str,
        extra_options: dict[str, str] | None,
    ) -> dict[str, str]:
        """Return placeholder values for rendering agent commands."""
        return {
            "prompt": prompt,
            "workdir": workdir,
            "model": model,
            "chat_id": chat_id,
            **(extra_options or {}),
        }

    def _render_template_value(
        self,
        template: str,
        context: dict[str, str],
    ) -> str:
        """Render supported ``{placeholder}`` values in a command field."""
        rendered = template
        for key, value in context.items():
            rendered = rendered.replace(f"{{{key}}}", value)
        return rendered

    def _build_credential_relation_operation_data(
        self,
        *,
        workspace,
        agent_definition_id: uuid.UUID,
        phase: str,
        prompt: str = "",
        model: str = "",
        workdir: str = "/workspace",
        chat_id: str = "",
        extra_options: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], list[dict]]:
        """Resolve relation default env and commands for an operation phase."""

        agent_def = self.agents.get_by_id(agent_definition_id)
        if agent_def is None:
            return {}, []

        workspace_service_ids = {
            credential.service_id
            for credential in workspace.credentials.all()
            if credential.service_id
        }
        if not workspace_service_ids:
            return {}, []

        relations = list(
            agent_def.credential_relations.select_related(
                "credential_service"
            ).prefetch_related("commands").filter(
                credential_service_id__in=workspace_service_ids
            ).order_by("credential_service__slug", "id")
        )
        if not relations:
            return {}, []

        render_context = self._build_render_context(
            prompt=prompt,
            model=model,
            workdir=workdir,
            chat_id=chat_id,
            extra_options=extra_options,
        )
        agent_default_env = dict(agent_def.default_env or {})
        merged_default_env: dict[str, str] = {}
        commands: list[dict] = []

        for relation in relations:
            relation_default_env = dict(relation.default_env or {})
            merged_default_env.update(relation_default_env)
            phase_commands = sorted(
                (
                    cmd
                    for cmd in relation.commands.all()
                    if cmd.phase == phase
                ),
                key=lambda cmd: cmd.order,
            )
            for cmd in phase_commands:
                command_env = {
                    **relation_default_env,
                    **agent_default_env,
                    **(cmd.env or {}),
                }
                commands.append(
                    {
                        "args": [
                            self._render_template_value(arg, render_context)
                            for arg in cmd.args
                        ],
                        "workdir": (
                            self._render_template_value(cmd.workdir, render_context)
                            if cmd.workdir
                            else workdir
                        ),
                        "env": command_env,
                        "description": cmd.description,
                    }
                )

        return merged_default_env, commands

    # ------------------------------------------------------------------
    # Workspace operations (initiated by REST API or frontend)
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_workspace_name(name: str, repos: list[str], workspace_id: uuid.UUID) -> str:
        """Return a non-empty workspace name from user input or sensible defaults."""
        trimmed = name.strip()
        if trimmed:
            return trimmed

        if repos:
            last_segment = repos[0].rstrip("/").split("/")[-1]
            cleaned = re.sub(r"\.git$", "", last_segment)
            if cleaned:
                return cleaned

        return f"workspace-{str(workspace_id)[:8]}"

    @staticmethod
    def _validate_runner_qemu_limits(runner: "Runner") -> None:
        """Validate min/max/default and total limits for a runner's QEMU config."""
        if runner.qemu_min_vcpus > runner.qemu_max_vcpus:
            raise ConflictError("Runner vCPU minimum cannot exceed maximum")
        if not (runner.qemu_min_vcpus <= runner.qemu_default_vcpus <= runner.qemu_max_vcpus):
            raise ConflictError("Runner default vCPU must be within min/max range")

        if runner.qemu_min_memory_mb > runner.qemu_max_memory_mb:
            raise ConflictError("Runner RAM minimum cannot exceed maximum")
        if not (runner.qemu_min_memory_mb <= runner.qemu_default_memory_mb <= runner.qemu_max_memory_mb):
            raise ConflictError("Runner default RAM must be within min/max range")

        if runner.qemu_min_disk_size_gb > runner.qemu_max_disk_size_gb:
            raise ConflictError("Runner disk minimum cannot exceed maximum")
        if not (runner.qemu_min_disk_size_gb <= runner.qemu_default_disk_size_gb <= runner.qemu_max_disk_size_gb):
            raise ConflictError("Runner default disk must be within min/max range")

        if runner.qemu_max_active_vcpus is not None and runner.qemu_max_active_vcpus < runner.qemu_default_vcpus:
            raise ConflictError("Runner total active vCPU limit cannot be smaller than default vCPU")
        if runner.qemu_max_active_memory_mb is not None and runner.qemu_max_active_memory_mb < runner.qemu_default_memory_mb:
            raise ConflictError("Runner total active RAM limit cannot be smaller than default RAM")
        if runner.qemu_max_active_disk_size_gb is not None and runner.qemu_max_active_disk_size_gb < runner.qemu_default_disk_size_gb:
            raise ConflictError("Runner total active disk limit cannot be smaller than default disk")

    @staticmethod
    def _task_workspace_operation(task_type: TaskType) -> WorkspaceOperation | None:
        """Map a task type to a generic blocking workspace operation."""
        return {
            TaskType.CREATE_WORKSPACE: WorkspaceOperation.CREATING,
            TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT: WorkspaceOperation.CREATING,
            TaskType.UPDATE_WORKSPACE: WorkspaceOperation.RESTARTING,
            TaskType.STOP_WORKSPACE: WorkspaceOperation.STOPPING,
            TaskType.RESUME_WORKSPACE: WorkspaceOperation.STARTING,
            TaskType.REMOVE_WORKSPACE: WorkspaceOperation.REMOVING,
            TaskType.CREATE_IMAGE_ARTIFACT: WorkspaceOperation.CAPTURING_IMAGE,
        }.get(task_type)

    @staticmethod
    def _workspace_operation_label(operation: str | None) -> str:
        """Return a readable operation label for conflict messages."""
        if not operation:
            return "busy"
        return operation.replace("_", " ")

    def _ensure_workspace_available(self, workspace: "Workspace") -> None:
        """Reject mutating operations while another blocking lifecycle action runs."""
        if workspace.active_operation:
            raise ConflictError(
                f"Workspace '{workspace.id}' is currently {self._workspace_operation_label(workspace.active_operation)}"
            )

    def _forward_workspace_operation(
        self,
        workspace_id: str,
        active_operation: str | None,
    ) -> None:
        """Forward workspace operation changes to subscribed frontend clients."""
        self._forward_to_frontend(
            "workspace:operation_changed",
            {
                "workspace_id": workspace_id,
                "active_operation": active_operation,
            },
            workspace_id,
        )

    async def _set_workspace_operation(
        self,
        workspace: "Workspace",
        operation: WorkspaceOperation | None,
    ) -> "Workspace":
        """Persist and broadcast a workspace operation change."""
        workspace = await sync_to_async(self.workspaces.update_active_operation)(
            workspace,
            operation,
        )
        self._forward_workspace_operation(str(workspace.id), workspace.active_operation)
        return workspace

    async def _dispatch_workspace_task(
        self,
        *,
        runner: "Runner",
        event: str,
        payload: dict,
        task: "Task",
        workspace: "Workspace" | None = None,
        operation: WorkspaceOperation | None = None,
    ) -> None:
        """Set busy state, emit the task to the runner, and roll back on dispatch failure."""
        try:
            if workspace is not None and operation is not None:
                await self._set_workspace_operation(workspace, operation)
            await self._emit_to_runner(runner, event, payload)
            await sync_to_async(self.tasks.mark_in_progress)(task)
        except Exception:
            if workspace is not None and operation is not None:
                await self._set_workspace_operation(workspace, None)
            raise

    @staticmethod
    def _resolve_qemu_resources(
        *,
        runner: "Runner",
        qemu_vcpus: int | None,
        qemu_memory_mb: int | None,
        qemu_disk_size_gb: int | None,
        current: tuple[int, int, int] | None = None,
    ) -> tuple[int, int, int]:
        """Resolve effective QEMU resources and validate against runner limits."""
        current_vcpus = current[0] if current else runner.qemu_default_vcpus
        current_memory_mb = current[1] if current else runner.qemu_default_memory_mb
        current_disk_size_gb = current[2] if current else runner.qemu_default_disk_size_gb

        resolved_vcpus = qemu_vcpus if qemu_vcpus is not None else current_vcpus
        resolved_memory_mb = qemu_memory_mb if qemu_memory_mb is not None else current_memory_mb
        resolved_disk_size_gb = qemu_disk_size_gb if qemu_disk_size_gb is not None else current_disk_size_gb

        if not (runner.qemu_min_vcpus <= resolved_vcpus <= runner.qemu_max_vcpus):
            raise ConflictError(
                f"vCPU value must be between {runner.qemu_min_vcpus} and {runner.qemu_max_vcpus}"
            )
        if not (runner.qemu_min_memory_mb <= resolved_memory_mb <= runner.qemu_max_memory_mb):
            raise ConflictError(
                f"RAM value must be between {runner.qemu_min_memory_mb} and {runner.qemu_max_memory_mb} MiB"
            )
        if not (runner.qemu_min_disk_size_gb <= resolved_disk_size_gb <= runner.qemu_max_disk_size_gb):
            raise ConflictError(
                f"Disk value must be between {runner.qemu_min_disk_size_gb} and {runner.qemu_max_disk_size_gb} GiB"
            )
        return resolved_vcpus, resolved_memory_mb, resolved_disk_size_gb

    @staticmethod
    def _ensure_runner_supports_runtime(
        *,
        runner: "Runner",
        runtime_type: str,
    ) -> None:
        """Raise when a runner does not advertise support for a runtime."""
        if runtime_type not in (runner.available_runtimes or []):
            raise ConflictError(f"Runner does not support runtime '{runtime_type}'")

    async def _ensure_qemu_active_capacity(
        self,
        *,
        runner: "Runner",
        requested_vcpus: int,
        requested_memory_mb: int,
        requested_disk_size_gb: int,
        exclude_workspace_id: uuid.UUID | None = None,
    ) -> None:
        """Ensure active QEMU aggregate limits allow the requested resources."""
        active_qemu = await sync_to_async(list)(
            self.workspaces.list_running_qemu_by_runner(runner.id)
        )

        total_vcpus = 0
        total_memory_mb = 0
        total_disk_size_gb = 0
        for ws in active_qemu:
            if exclude_workspace_id and ws.id == exclude_workspace_id:
                continue
            total_vcpus += ws.qemu_vcpus or runner.qemu_default_vcpus
            total_memory_mb += ws.qemu_memory_mb or runner.qemu_default_memory_mb
            total_disk_size_gb += ws.qemu_disk_size_gb or runner.qemu_default_disk_size_gb

        next_total_vcpus = total_vcpus + requested_vcpus
        next_total_memory_mb = total_memory_mb + requested_memory_mb
        next_total_disk_size_gb = total_disk_size_gb + requested_disk_size_gb

        if (
            runner.qemu_max_active_vcpus is not None
            and next_total_vcpus > runner.qemu_max_active_vcpus
        ):
            raise ConflictError(
                f"Runner active vCPU limit exceeded ({next_total_vcpus}/{runner.qemu_max_active_vcpus})"
            )
        if (
            runner.qemu_max_active_memory_mb is not None
            and next_total_memory_mb > runner.qemu_max_active_memory_mb
        ):
            raise ConflictError(
                f"Runner active RAM limit exceeded ({next_total_memory_mb}/{runner.qemu_max_active_memory_mb} MiB)"
            )
        if (
            runner.qemu_max_active_disk_size_gb is not None
            and next_total_disk_size_gb > runner.qemu_max_active_disk_size_gb
        ):
            raise ConflictError(
                f"Runner active disk limit exceeded ({next_total_disk_size_gb}/{runner.qemu_max_active_disk_size_gb} GiB)"
            )

    async def create_workspace(
        self,
        *,
        name: str,
        repos: list[str],
        runtime_type: str = "docker",
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
        credentials: list | None = None,
        runner_id: uuid.UUID | None = None,
        image_artifact_id: uuid.UUID | None = None,
        user=None,
        organization_id: uuid.UUID | None = None,
    ) -> tuple["Workspace", "Task"]:
        """
        Create a new workspace on a runner.

        Agent type is no longer required at workspace creation time — it is
        selected when creating the first chat. If runner_id is not specified,
        any online runner in the organization is selected.

        Returns the created Workspace and Task records.
        """
        if image_artifact_id is None:
            raise ConflictError("An image artifact is required")

        selected_artifact = await sync_to_async(self.image_artifacts.get_by_id)(
            image_artifact_id
        )
        if selected_artifact is None:
            raise NotFoundError("ImageArtifact", str(image_artifact_id))

        if selected_artifact.status != "ready":
            raise ConflictError(f"Image artifact '{image_artifact_id}' is not ready")

        selected_runner_build = selected_artifact.runner_image_build
        source_workspace = selected_artifact.source_workspace
        requested_runner_id = runner_id
        if selected_runner_build is not None:
            if selected_runner_build.status != "active":
                raise ConflictError("Selected image artifact is not active on runner")
            if (
                organization_id
                and selected_runner_build.runner.organization_id != organization_id
            ):
                raise NotFoundError("ImageArtifact", str(image_artifact_id))
            if (
                requested_runner_id is not None
                and requested_runner_id != selected_runner_build.runner_id
            ):
                raise ConflictError(
                    "Selected runner does not have the selected image artifact"
                )
            runtime_type = selected_runner_build.image_definition.runtime_type
            runner_id = selected_runner_build.runner_id
        else:
            if source_workspace is None:
                raise ConflictError("Captured image artifact is missing its source workspace")
            if (
                organization_id
                and source_workspace.runner.organization_id != organization_id
            ):
                raise NotFoundError("ImageArtifact", str(image_artifact_id))
            if (
                requested_runner_id is not None
                and requested_runner_id != source_workspace.runner_id
            ):
                raise ConflictError(
                    "Selected runner does not have the selected image artifact"
                )
            runtime_type = source_workspace.runtime_type
            runner_id = source_workspace.runner_id

        # Find a suitable runner
        if runner_id:
            runner = await sync_to_async(self.runners.get_by_id)(runner_id)
            if runner is None:
                raise RunnerNotFoundError(str(runner_id))
            if not runner.is_online:
                raise RunnerOfflineError(str(runner_id))
            # Verify runner belongs to the organization
            if organization_id and runner.organization_id != organization_id:
                raise RunnerNotFoundError(str(runner_id))
        else:
            # Pick any online runner (no agent filter needed)
            runners_qs = self.runners.list_by_organization(organization_id).filter(
                status=RunnerStatus.ONLINE
            ) if organization_id else self.runners.list_online()
            runner = await sync_to_async(lambda: runners_qs.first())()
            if runner is None:
                raise NoAvailableRunnerError("any")

        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=runtime_type,
        )

        self._validate_runner_qemu_limits(runner)
        resolved_qemu_vcpus: int | None = None
        resolved_qemu_memory_mb: int | None = None
        resolved_qemu_disk_size_gb: int | None = None
        if runtime_type == RuntimeType.QEMU:
            (
                resolved_qemu_vcpus,
                resolved_qemu_memory_mb,
                resolved_qemu_disk_size_gb,
            ) = self._resolve_qemu_resources(
                runner=runner,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
            )
            await self._ensure_qemu_active_capacity(
                runner=runner,
                requested_vcpus=resolved_qemu_vcpus,
                requested_memory_mb=resolved_qemu_memory_mb,
                requested_disk_size_gb=resolved_qemu_disk_size_gb,
            )

        # Create records
        workspace_id = generate_uuid()
        workspace_name = self._derive_workspace_name(name, repos, workspace_id)
        workspace = await sync_to_async(self.workspaces.create)(
            workspace_id=workspace_id,
            runner=runner,
            name=workspace_name,
            runtime_type=runtime_type,
            qemu_vcpus=resolved_qemu_vcpus,
            qemu_memory_mb=resolved_qemu_memory_mb,
            qemu_disk_size_gb=resolved_qemu_disk_size_gb,
            created_by=user,
        )
        if credentials is not None:
            await sync_to_async(self.workspaces.set_credentials)(workspace, credentials)

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.CREATE_WORKSPACE,
            workspace=workspace,
        )

        # Dispatch to runner — include workspace_id so the runner
        # uses the same UUID the backend assigned. No configure_commands
        # at this stage — they run on first chat usage.
        await self._dispatch_workspace_task(
            runner=runner,
            event="task:create_workspace",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.CREATE_WORKSPACE),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "repos": repos,
                "runtime_type": runtime_type,
                "qemu_vcpus": resolved_qemu_vcpus,
                "qemu_memory_mb": resolved_qemu_memory_mb,
                "qemu_disk_size_gb": resolved_qemu_disk_size_gb,
                "configure_commands": [],
                "env_vars": env_vars or {},
                "ssh_keys": ssh_keys or [],
                "image_artifact_id": str(image_artifact_id),
                "image_tag": (
                    selected_runner_build.image_tag
                    if selected_runner_build and runtime_type == RuntimeType.DOCKER
                    else ""
                ),
                "base_image_path": (
                    selected_runner_build.image_path
                    if selected_runner_build and runtime_type == RuntimeType.QEMU
                    else ""
                ),
            },
        )
        logger.info(
            "Dispatched create_workspace to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        return workspace, task

    async def run_prompt(
        self,
        workspace_id: uuid.UUID,
        prompt: str,
        agent_model: str | None = None,
        agent_options: dict[str, str] | None = None,
        chat_id: str | None = None,
        skill_ids: list[uuid.UUID] | None = None,
    ) -> tuple["Session", "Task", "Chat"]:
        """
        Run a prompt in an existing workspace.

        Creates a Session and Task, then dispatches to the runner.
        If the agent supports multi-chat and no chat_id is provided,
        a new chat is created automatically.

        ``agent_options`` is a dict of option key/value pairs (e.g.
        ``{"model": "claude-opus-4-6", "permission_mode": "plan"}``).
        The legacy ``agent_model`` parameter is still accepted for
        backwards compatibility; if both are provided, ``agent_options``
        takes precedence for the model key.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to run prompts"
            )

        has_active = await sync_to_async(self.sessions.has_active_for_workspace)(
            workspace_id
        )
        if has_active:
            raise ConflictError("Workspace already has an active session")

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        # Determine agent definition from the chat
        chat_agent_definition_id: uuid.UUID | None = None
        if chat_id:
            _temp_chat = await sync_to_async(self.chats.get_by_id)(uuid.UUID(chat_id))
            if _temp_chat:
                chat_agent_definition_id = _temp_chat.agent_definition_id

        if chat_agent_definition_id is None:
            raise ValueError("No agent definition defined for this chat")

        agent_def = await sync_to_async(self.agents.get_by_id)(chat_agent_definition_id)
        if agent_def is None:
            raise ValueError(
                f"Unknown agent definition for chat '{chat_id}': '{chat_agent_definition_id}'"
            )

        # Merge legacy agent_model into agent_options for unified handling
        resolved_options: dict[str, str] = dict(agent_options or {})
        if agent_model and "model" not in resolved_options:
            resolved_options["model"] = agent_model.strip()

        # Resolve defaults for any available_options defined on the agent
        available_opt_defs: list[dict] = list(agent_def.available_options or [])
        for opt_def in available_opt_defs:
            key = opt_def.get("key", "")
            if not key:
                continue
            choices: list[str] = opt_def.get("choices", [])
            default: str = opt_def.get("default", choices[0] if choices else "")
            if key not in resolved_options or not resolved_options[key]:
                # Fall back to last session's value for this key
                if key == "model":
                    latest_session = await sync_to_async(
                        self.sessions.get_latest_for_workspace
                    )(workspace_id)
                    latest_val = (
                        (latest_session.agent_options or {}).get("model", "")
                        if latest_session else ""
                    ).strip()
                    resolved_options[key] = (
                        latest_val if (latest_val and (not choices or latest_val in choices))
                        else default
                    )
                else:
                    resolved_options[key] = default
            elif choices and resolved_options[key] not in choices:
                raise ValueError(
                    f"Option '{key}' value '{resolved_options[key]}' is not in "
                    f"allowed choices for agent '{agent_def.name}'"
                )

        selected_model = resolved_options.get("model", "")

        # Resolve or create chat
        chat = await self._resolve_chat(
            workspace, agent_def, chat_id, prompt
        )
        await self._assert_chat_is_writable(workspace, agent_def, chat)

        # Build augmented prompt: append skill bodies for the runner.
        # The session record stores only the original user prompt.
        effective_prompt = prompt
        resolved_skills = []
        if skill_ids:
            from apps.skills.repositories import SkillRepository
            resolved_skills = await sync_to_async(SkillRepository.get_many_by_ids)(
                skill_ids
            )
            if resolved_skills:
                appendix = "\n\n---\n# Follow these instructions (skills) carefully:\n\n"
                appendix += "\n\n".join(
                    f"## {s.name}\n\n{s.body}" for s in resolved_skills
                )
                effective_prompt = prompt + appendix

        # Detect whether this is the first message in the chat so agents that
        # need different initialisation (e.g. Claude Code's --session-id flag)
        # can use their run_first command template.
        #
        # We intentionally ignore failed sessions: if all prior sessions in
        # this chat failed (e.g. the runner went offline mid-conversation),
        # the next prompt should be treated as a fresh first message so the
        # correct agent initialisation command is used.
        is_first_message = not await sync_to_async(
            self.sessions.has_any_successful_for_chat
        )(chat.id)

        # Build the run command from the agent definition in DB
        workspace_credentials = await sync_to_async(
            CredentialSvc().resolve_workspace_credentials
        )(workspace)
        relation_default_env, relation_preflight_commands = await sync_to_async(
            self._build_credential_relation_operation_data
        )(
            workspace=workspace,
            agent_definition_id=agent_def.id,
            phase=AgentCommandPhase.CONFIGURE,
            prompt=effective_prompt,
            model=selected_model,
            chat_id=str(chat.id),
            extra_options=resolved_options,
        )
        run_command = await sync_to_async(self.build_run_command)(
            agent_def.id,
            effective_prompt,
            selected_model,
            chat_id=str(chat.id),
            is_first_message=is_first_message,
            extra_options=resolved_options,
            additional_default_env=relation_default_env,
        )
        run_command = self._merge_workspace_env_vars(
            run_command,
            workspace_credentials.env_vars,
        )

        configure_commands: list[dict] = [
            self._merge_workspace_env_vars(cmd, workspace_credentials.env_vars)
            for cmd in relation_preflight_commands
        ]
        fallback_configure_commands = await sync_to_async(self.get_configure_commands)(
            agent_def.id
        )
        fallback_configure_commands = [
            self._merge_workspace_env_vars(cmd, workspace_credentials.env_vars)
            for cmd in fallback_configure_commands
        ]

        # Create session and task
        session_id = generate_uuid()
        session = await sync_to_async(self.sessions.create)(
            session_id=session_id,
            prompt=prompt,  # store original prompt, not augmented
            agent_model=selected_model,
            agent_options=resolved_options,
            chat=chat,
        )

        # Snapshot skills immediately after session creation
        if resolved_skills:
            from apps.skills.repositories import SessionSkillRepository
            await sync_to_async(SessionSkillRepository.create_snapshots)(
                session, resolved_skills
            )

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.RUN_PROMPT,
            workspace=workspace,
            session=session,
        )

        # Dispatch — include configure_commands if this agent needs first-time setup
        await self._emit_to_runner(
            runner,
            "task:run_prompt",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "prompt": prompt,
                "command": run_command,
                "configure_commands": configure_commands,
                "fallback_configure_commands": fallback_configure_commands,
                "env_vars": workspace_credentials.env_vars,
                "ssh_keys": workspace_credentials.ssh_keys,
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched run_prompt to runner %s (workspace=%s, chat=%s, task=%s)",
            runner.id,
            workspace_id,
            chat.id,
            task_id,
        )
        return session, task, chat

    async def _resolve_chat(
        self,
        workspace,
        agent_def,
        chat_id: str | None,
        prompt: str,
    ):
        """Resolve or create the chat for a prompt dispatch.

        - If chat_id is given: use that chat (validate it belongs to workspace).
        - If agent supports multi-chat and no chat_id: create a new chat.
        - If agent does NOT support multi-chat: use the single implicit chat
          (create if needed).
        """
        if chat_id:
            chat = await sync_to_async(self.chats.get_by_id)(uuid.UUID(chat_id))
            if chat is None or chat.workspace_id != workspace.id:
                raise ValueError(f"Chat '{chat_id}' not found in workspace '{workspace.id}'")
            return chat

        if agent_def.supports_multi_chat:
            # Create a new chat for each prompt without an explicit chat_id
            chat_name = prompt[:50] + "…" if len(prompt) > 50 else prompt
            new_chat = await sync_to_async(self.chats.create)(
                chat_id=generate_uuid(),
                workspace=workspace,
                name=chat_name,
                agent_definition=agent_def,
                agent_type=agent_def.name,
            )
            return new_chat
        else:
            # Single-chat agent: reuse latest chat for this agent.
            latest_chat = await sync_to_async(
                self.chats.get_latest_for_workspace_agent
            )(workspace.id, agent_def.id)
            if latest_chat:
                return latest_chat
            # Create the first (and only) chat
            return await sync_to_async(self.chats.create)(
                chat_id=generate_uuid(),
                workspace=workspace,
                name="Chat",
                agent_definition=agent_def,
                agent_type=agent_def.name,
            )

    async def _assert_chat_is_writable(self, workspace, agent_def, chat) -> None:
        """Reject writes to stale chats for single-chat agents."""
        if agent_def.supports_multi_chat:
            return

        latest_chat = await sync_to_async(self.chats.get_latest_for_workspace_agent)(
            workspace.id,
            agent_def.id,
        )
        if latest_chat is None:
            return
        if latest_chat.id != chat.id:
            raise ConflictError(
                "This chat is locked because a newer chat exists for this agent. "
                "Please use the latest chat."
            )

    async def update_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        name: str | None = None,
        credentials: list | None = None,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
    ) -> "Workspace":
        """Update mutable workspace metadata and attached credentials."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        if name is not None:
            trimmed = name.strip()
            if not trimmed:
                raise ValueError("Workspace name must not be empty")
            workspace = await sync_to_async(self.workspaces.update_name)(workspace, trimmed)

        if credentials is not None:
            workspace = await sync_to_async(self.workspaces.set_credentials)(
                workspace,
                credentials,
            )

        self._ensure_workspace_available(workspace)

        qemu_fields_requested = any(
            value is not None
            for value in (qemu_vcpus, qemu_memory_mb, qemu_disk_size_gb)
        )
        if qemu_fields_requested:
            if workspace.runtime_type != RuntimeType.QEMU:
                raise ValueError("QEMU resources can only be set for QEMU workspaces")
            if workspace.status not in (WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED):
                raise WorkspaceStateError(
                    f"Workspace '{workspace_id}' is '{workspace.status}', must be running or stopped to reconfigure resources"
                )

            runner = workspace.runner
            self._validate_runner_qemu_limits(runner)
            current = (
                workspace.qemu_vcpus or runner.qemu_default_vcpus,
                workspace.qemu_memory_mb or runner.qemu_default_memory_mb,
                workspace.qemu_disk_size_gb or runner.qemu_default_disk_size_gb,
            )
            (
                resolved_qemu_vcpus,
                resolved_qemu_memory_mb,
                resolved_qemu_disk_size_gb,
            ) = self._resolve_qemu_resources(
                runner=runner,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
                current=current,
            )
            await self._ensure_qemu_active_capacity(
                runner=runner,
                requested_vcpus=resolved_qemu_vcpus,
                requested_memory_mb=resolved_qemu_memory_mb,
                requested_disk_size_gb=resolved_qemu_disk_size_gb,
                exclude_workspace_id=workspace.id,
            )

            workspace = await sync_to_async(self.workspaces.update_qemu_resources)(
                workspace,
                qemu_vcpus=resolved_qemu_vcpus,
                qemu_memory_mb=resolved_qemu_memory_mb,
                qemu_disk_size_gb=resolved_qemu_disk_size_gb,
            )

            if workspace.status == WorkspaceStatus.RUNNING:
                runner = workspace.runner
                if not runner.is_online:
                    raise RunnerOfflineError(str(runner.id))

                task_id = generate_uuid()
                task = await sync_to_async(self.tasks.create)(
                    task_id=task_id,
                    runner=runner,
                    task_type=TaskType.UPDATE_WORKSPACE,
                    workspace=workspace,
                )
                await self._dispatch_workspace_task(
                    runner=runner,
                    event="task:update_workspace",
                    task=task,
                    workspace=workspace,
                    operation=self._task_workspace_operation(TaskType.UPDATE_WORKSPACE),
                    payload={
                        "task_id": str(task_id),
                        "workspace_id": str(workspace_id),
                        "qemu_vcpus": resolved_qemu_vcpus,
                        "qemu_memory_mb": resolved_qemu_memory_mb,
                        "qemu_disk_size_gb": resolved_qemu_disk_size_gb,
                    },
                )

        return await sync_to_async(self.workspaces.get_by_id)(workspace_id)

    async def rename_workspace(self, workspace_id: uuid.UUID, name: str) -> "Workspace":
        """Rename an existing workspace."""
        return await self.update_workspace(workspace_id, name=name)

    async def stop_workspace(self, workspace_id: uuid.UUID) -> "Task":
        """Stop a running workspace."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to stop"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.STOP_WORKSPACE,
            workspace=workspace,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:stop_workspace",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.STOP_WORKSPACE),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
            },
        )
        return task

    async def cancel_session_prompt(
        self,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> "Task":
        """Cancel a running prompt session without stopping the workspace."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        session = await sync_to_async(self.sessions.get_by_id)(session_id)
        if session is None or session.chat.workspace_id != workspace_id:
            raise ValueError(
                f"Session '{session_id}' not found in workspace '{workspace_id}'"
            )

        if session.status not in [SessionStatus.PENDING, SessionStatus.RUNNING]:
            raise ConflictError(
                f"Session '{session_id}' is '{session.status}' and cannot be cancelled"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        run_task = await sync_to_async(self.tasks.get_active_run_task_for_session)(
            session_id
        )
        if run_task is None:
            raise ConflictError(
                f"No active run task found for session '{session_id}'"
            )

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.CANCEL_SESSION,
            workspace=workspace,
            session=session,
        )

        await self._emit_to_runner(
            runner,
            "task:cancel_prompt",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "target_task_id": str(run_task.id),
                "session_id": str(session_id),
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        return task

    async def resume_workspace(self, workspace_id: uuid.UUID) -> "Task":
        """Resume a stopped workspace."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.STOPPED:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.STOPPED}' to resume"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=workspace.runtime_type,
        )

        qemu_vcpus = workspace.qemu_vcpus
        qemu_memory_mb = workspace.qemu_memory_mb
        qemu_disk_size_gb = workspace.qemu_disk_size_gb
        if workspace.runtime_type == RuntimeType.QEMU:
            self._validate_runner_qemu_limits(runner)
            (
                qemu_vcpus,
                qemu_memory_mb,
                qemu_disk_size_gb,
            ) = self._resolve_qemu_resources(
                runner=runner,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
            )
            await self._ensure_qemu_active_capacity(
                runner=runner,
                requested_vcpus=qemu_vcpus,
                requested_memory_mb=qemu_memory_mb,
                requested_disk_size_gb=qemu_disk_size_gb,
                exclude_workspace_id=workspace.id,
            )

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.RESUME_WORKSPACE,
            workspace=workspace,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:resume_workspace",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.RESUME_WORKSPACE),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "qemu_vcpus": qemu_vcpus,
                "qemu_memory_mb": qemu_memory_mb,
                "qemu_disk_size_gb": qemu_disk_size_gb,
            },
        )
        return task

    async def remove_workspace(self, workspace_id: uuid.UUID) -> "Task":
        """Remove a workspace and its container."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.REMOVE_WORKSPACE,
            workspace=workspace,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:remove_workspace",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.REMOVE_WORKSPACE),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
            },
        )
        return task

    # ------------------------------------------------------------------
    # Event handlers (called by Socket.IO consumer when runner reports back)
    # ------------------------------------------------------------------

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

    def handle_workspace_created(
        self,
        task_id: str,
        workspace_id: str,
        status: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle workspace:created event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:created for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace is None:
            logger.warning("Task %s has no associated workspace", task_id)
            return

        self.workspaces.update_status(workspace, WorkspaceStatus.RUNNING)
        self.workspaces.update_active_operation(workspace, None)
        self.tasks.complete(task)
        logger.info("Workspace created: %s", workspace_id)

        self._forward_to_frontend(
            "workspace:status_changed",
            {"workspace_id": workspace_id, "status": "running", "task_id": task_id},
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def handle_workspace_stopped(
        self, task_id: str, workspace_id: str, runner_id: str | None = None
    ) -> None:
        """Handle workspace:stopped event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:stopped for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.update_status(workspace, WorkspaceStatus.STOPPED)
            self.workspaces.update_active_operation(workspace, None)
        self._cleanup_desktop_state(workspace_id)
        self.tasks.complete(task)
        logger.info("Workspace stopped: %s", workspace_id)

        self._forward_to_frontend(
            "workspace:status_changed",
            {"workspace_id": workspace_id, "status": "stopped", "task_id": task_id},
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def handle_workspace_resumed(
        self, task_id: str, workspace_id: str, runner_id: str | None = None
    ) -> None:
        """Handle workspace:resumed event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:resumed for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.update_status(workspace, WorkspaceStatus.RUNNING)
            self.workspaces.update_active_operation(workspace, None)
        self.tasks.complete(task)
        logger.info("Workspace resumed: %s", workspace_id)

        self._forward_to_frontend(
            "workspace:status_changed",
            {"workspace_id": workspace_id, "status": "running", "task_id": task_id},
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def handle_workspace_updated(
        self, task_id: str, workspace_id: str, runner_id: str | None = None
    ) -> None:
        """Handle workspace:updated event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:updated for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.update_active_operation(workspace, None)
        self.tasks.complete(task)
        logger.info("Workspace updated: %s", workspace_id)
        self._forward_workspace_operation(workspace_id, None)

    def handle_workspace_error(
        self, task_id: str, error: str, runner_id: str | None = None
    ) -> None:
        """Handle workspace:error event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:error for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        # If this was a workspace lifecycle creation task, mark workspace failed.
        workspace = task.workspace
        workspace_id = str(workspace.id) if workspace else None
        if workspace:
            self.workspaces.update_active_operation(workspace, None)
        if workspace and task.type in {
            TaskType.CREATE_WORKSPACE,
            TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT,
        }:
            self.workspaces.update_status(workspace, WorkspaceStatus.FAILED)

        self.tasks.fail(task, error)
        logger.error("Workspace error (task=%s): %s", task_id, error)

        if workspace_id:
            self._forward_workspace_operation(workspace_id, None)
            self._forward_to_frontend(
                "workspace:error",
                {"workspace_id": workspace_id, "task_id": task_id, "error": error},
                workspace_id,
            )

    def handle_output_chunk(
        self,
        task_id: str,
        workspace_id: str,
        line: str,
        runner_id: str | None = None,
    ) -> None:
        """
        Handle output:chunk event from a runner.

        Appends the line to the session output and forwards the chunk
        to subscribed frontend clients via Socket.IO.
        """
        # Normalize line endings: some runtimes (e.g. QEMU/asyncssh) include
        # a trailing newline in each yielded line. Strip it so that
        # append_output (which joins lines with "\n") does not produce
        # double newlines in the stored output.
        line = line.rstrip("\r\n")

        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            return

        if not self._validate_task_runner(task, runner_id):
            return

        session = task.session
        if session:
            self.sessions.append_output(session, line)

        session_id = str(session.id) if session else None
        chat_id = str(session.chat_id) if session and session.chat_id else None
        self._forward_to_frontend(
            "session:output_chunk",
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "chat_id": chat_id,
                "task_id": task_id,
                "line": line,
            },
            workspace_id,
        )

    def handle_output_status(
        self,
        task_id: str,
        workspace_id: str,
        status: str,
        detail: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle output:status event from a runner and forward to frontend."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            return

        if not self._validate_task_runner(task, runner_id):
            return

        session = task.session
        session_id = str(session.id) if session else None
        chat_id = str(session.chat_id) if session and session.chat_id else None
        self._forward_to_frontend(
            "session:status",
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "chat_id": chat_id,
                "task_id": task_id,
                "status": status,
                "detail": detail,
            },
            workspace_id,
        )

    def handle_output_complete(
        self, task_id: str, workspace_id: str, runner_id: str | None = None
    ) -> None:
        """Handle output:complete event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning(
                "Received output:complete for unknown task: %s", task_id
            )
            return

        if not self._validate_task_runner(task, runner_id):
            return

        session = task.session
        session_id = str(session.id) if session else None
        chat_id = str(session.chat_id) if session and session.chat_id else None
        if session:
            self.sessions.complete(session)

        self.tasks.complete(task)
        logger.info(
            "Prompt completed (task=%s, workspace=%s)", task_id, workspace_id
        )

        self._forward_to_frontend(
            "session:completed",
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "chat_id": chat_id,
                "task_id": task_id,
            },
            workspace_id,
        )

    def handle_output_error(
        self,
        task_id: str,
        workspace_id: str,
        error: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle output:error event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning(
                "Received output:error for unknown task: %s", task_id
            )
            return

        if not self._validate_task_runner(task, runner_id):
            return

        session = task.session
        session_id = str(session.id) if session else None
        chat_id = str(session.chat_id) if session and session.chat_id else None
        normalized_error = error.strip()
        is_user_cancelled = (
            normalized_error.casefold()
            == USER_CANCELLED_PROMPT_ERROR.casefold()
        )
        if session:
            failed_output = (
                f"{session.output}\n[Error] {normalized_error}"
                if session.output
                else f"[Error] {normalized_error}"
            )
            self.sessions.fail(
                session,
                output=failed_output,
                error_message=None if is_user_cancelled else normalized_error,
            )

        self.tasks.fail(task, normalized_error)
        logger.error(
            "Prompt error (task=%s, workspace=%s): %s",
            task_id,
            workspace_id,
            normalized_error,
        )

        self._forward_to_frontend(
            "session:failed",
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "chat_id": chat_id,
                "task_id": task_id,
                "error": normalized_error,
            },
            workspace_id,
        )

    def handle_prompt_cancelled(
        self,
        task_id: str,
        workspace_id: str,
        target_task_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle prompt:cancelled from runner and complete cancel task."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning(
                "Received prompt:cancelled for unknown task: %s", task_id
            )
            return

        if not self._validate_task_runner(task, runner_id):
            return

        self.tasks.complete(task)
        logger.info(
            "Prompt cancellation completed (task=%s, workspace=%s, target=%s)",
            task_id,
            workspace_id,
            target_task_id,
        )

    def handle_workspace_removed(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle workspace:removed event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning(
                "Received workspace:removed for unknown task: %s", task_id
            )
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.update_status(workspace, WorkspaceStatus.REMOVED)
            self.workspaces.update_active_operation(workspace, None)
        self._cleanup_desktop_state(workspace_id)

        self.tasks.complete(task)
        logger.info("Workspace removed: %s", workspace_id)

        self._forward_to_frontend(
            "workspace:status_changed",
            {
                "workspace_id": workspace_id,
                "status": "removed",
                "task_id": task_id,
            },
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    # ------------------------------------------------------------------
    # Terminal (interactive PTY)
    # ------------------------------------------------------------------

    # In-memory mapping: workspace_id (str) → terminal_id (str)
    _active_terminals: dict[str, str] = {}
    # In-memory mapping: workspace_id (str) → runner_id (str)
    # Populated when a terminal starts so terminal:output can be validated
    # without a DB lookup on every single chunk.
    _terminal_workspace_runner: dict[str, str] = {}

    # In-memory desktop session state
    _active_desktops: dict[str, dict] = {}
    _desktop_workspace_runner: dict[str, str] = {}

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
    ) -> "Task":
        """Dispatch a start_terminal task to the runner.

        Returns the Task record.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to start a terminal"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.START_TERMINAL,
            workspace=workspace,
        )

        workspace_credentials = await sync_to_async(
            CredentialSvc().resolve_workspace_credentials
        )(workspace)
        latest_chat = await sync_to_async(
            lambda: self.chats.list_by_workspace(workspace.id)
            .order_by("-created_at", "-id")
            .first()
        )()
        terminal_agent_definition_id = latest_chat.agent_definition_id if latest_chat else None
        configure_commands: list[dict] = []
        if terminal_agent_definition_id:
            _, relation_preflight_commands = await sync_to_async(
                self._build_credential_relation_operation_data
            )(
                workspace=workspace,
                agent_definition_id=terminal_agent_definition_id,
                phase=AgentCommandPhase.CONFIGURE,
            )
            configure_commands = [
                self._merge_workspace_env_vars(cmd, workspace_credentials.env_vars)
                for cmd in relation_preflight_commands
            ]

        await self._emit_to_runner(
            runner,
            "task:start_terminal",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "cols": cols,
                "rows": rows,
                "configure_commands": configure_commands,
                "env_vars": workspace_credentials.env_vars,
                "ssh_keys": workspace_credentials.ssh_keys,
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched start_terminal to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        return task

    async def handle_terminal_started(
        self,
        task_id: str,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:started event from a runner."""
        from .sio_server import emit_to_frontend

        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self._validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        self._active_terminals[workspace_id] = terminal_id
        # Cache runner ownership so terminal:output validation is O(1)
        if runner_id:
            self._terminal_workspace_runner[workspace_id] = runner_id
        logger.info(
            "Terminal started: workspace=%s, terminal=%s",
            workspace_id,
            terminal_id,
        )

        await emit_to_frontend(
            "terminal:started",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "task_id": task_id,
            },
            workspace_id,
        )

    async def handle_terminal_output(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:output from runner — forward to frontend (no DB).

        Runner ownership is validated against the in-memory cache populated
        when the terminal session was started, so no DB lookup is required
        on the hot path.
        """
        if runner_id:
            cached = self._terminal_workspace_runner.get(workspace_id)
            if cached is not None and cached != runner_id:
                logger.warning(
                    "terminal:output rejected: workspace %s is owned by "
                    "runner %s, not %s",
                    workspace_id,
                    cached,
                    runner_id,
                )
                return

        from .sio_server import emit_to_frontend

        await emit_to_frontend(
            "terminal:output",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "data": data,
            },
            workspace_id,
        )

    async def handle_terminal_closed(
        self,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:closed from runner."""
        if runner_id:
            cached = self._terminal_workspace_runner.get(workspace_id)
            if cached is not None and cached != runner_id:
                logger.warning(
                    "terminal:closed rejected: workspace %s is owned by "
                    "runner %s, not %s",
                    workspace_id,
                    cached,
                    runner_id,
                )
                return

        from .sio_server import emit_to_frontend

        self._active_terminals.pop(workspace_id, None)
        self._terminal_workspace_runner.pop(workspace_id, None)
        logger.info(
            "Terminal closed: workspace=%s, terminal=%s",
            workspace_id,
            terminal_id,
        )
        await emit_to_frontend(
            "terminal:closed",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
            },
            workspace_id,
        )

    async def forward_terminal_input(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
    ) -> None:
        """Forward terminal input from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:input",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "data": data,
            },
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    async def forward_terminal_resize(
        self,
        workspace_id: str,
        terminal_id: str,
        cols: int,
        rows: int,
    ) -> None:
        """Forward terminal resize from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:resize",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "cols": cols,
                "rows": rows,
            },
        )

    async def forward_terminal_close(
        self,
        workspace_id: str,
        terminal_id: str,
    ) -> None:
        """Forward terminal close request from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:close",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
            },
        )

    # ------------------------------------------------------------------
    # Desktop session (KasmVNC)
    # ------------------------------------------------------------------

    async def start_desktop(
        self,
        workspace_id: uuid.UUID,
    ) -> "Task":
        """Dispatch a start_desktop task to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to start a desktop"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.START_DESKTOP,
            workspace=workspace,
        )

        await self._emit_to_runner(
            runner,
            "task:start_desktop",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched start_desktop to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        return task

    async def stop_desktop(
        self,
        workspace_id: uuid.UUID,
    ) -> "Task":
        """Dispatch a stop_desktop task to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.STOP_DESKTOP,
            workspace=workspace,
        )

        await self._emit_to_runner(
            runner,
            "task:stop_desktop",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched stop_desktop to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        return task

    async def handle_desktop_started(
        self,
        task_id: str | None,
        workspace_id: str,
        port: int,
        container_ip: str,
        network_name: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle desktop:started event from a runner."""
        from .sio_server import emit_to_frontend

        task = None
        if task_id:
            task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
            if task and not self._validate_task_runner(task, runner_id):
                return

        desktop_state = {
            "port": port,
            "container_ip": container_ip,
            "network_name": network_name,
        }

        try:
            if network_name:
                await self._connect_backend_to_workspace_network(network_name)
            self._record_active_desktop(
                workspace_id,
                desktop_state,
                runner_id=runner_id,
            )
        except Exception as exc:
            if task:
                await sync_to_async(self.tasks.fail)(task, str(exc))
            await emit_to_frontend(
                "workspace:error",
                {
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "error": str(exc),
                },
                workspace_id,
            )
            return

        if task:
            await sync_to_async(self.tasks.complete)(task)

        logger.info(
            "Desktop started: workspace=%s, port=%s, ip=%s",
            workspace_id,
            port,
            container_ip,
        )

        await emit_to_frontend(
            "desktop:started",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "proxy_url": f"/ws/desktop/{workspace_id}/",
            },
            workspace_id,
        )

    async def handle_desktop_stopped(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle desktop:stopped event from a runner."""
        from .sio_server import emit_to_frontend

        if runner_id:
            cached = self._desktop_workspace_runner.get(workspace_id)
            if cached is not None and cached != runner_id:
                logger.warning(
                    "desktop:stopped rejected: workspace %s is owned by "
                    "runner %s, not %s",
                    workspace_id,
                    cached,
                    runner_id,
                )
                return

        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self._validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        desktop_info = self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)

        # Disconnect backend from the workspace network
        if desktop_info:
            try:
                await self._disconnect_backend_from_workspace_network(
                    desktop_info["network_name"]
                )
            except Exception:
                logger.exception(
                    "Failed to disconnect backend from workspace network %s",
                    desktop_info.get("network_name"),
                )

        logger.info("Desktop stopped: workspace=%s", workspace_id)

        await emit_to_frontend(
            "desktop:stopped",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
            },
            workspace_id,
        )

    def is_desktop_active(self, workspace_id: str) -> bool:
        """Check if a desktop session is active for a workspace."""
        if workspace_id in self._active_desktops:
            return True
        return self.recover_desktop_state(workspace_id) is not None

    def get_desktop_info(self, workspace_id: str) -> dict | None:
        """Get desktop session info (container_ip, port, network) if active."""
        desktop_info = self._active_desktops.get(workspace_id)
        if desktop_info is not None:
            return desktop_info
        return self.recover_desktop_state(workspace_id)

    def recover_desktop_state(self, workspace_id: str) -> dict | None:
        """Best-effort recovery for Docker desktop sessions after backend restarts."""
        from .models import Workspace

        try:
            workspace = Workspace.objects.select_related("runner").get(id=workspace_id)
        except (Workspace.DoesNotExist, ValueError):
            return None

        if workspace.runtime_type != RuntimeType.DOCKER:
            return None
        if workspace.status != WorkspaceStatus.RUNNING:
            return None

        import docker as docker_sdk

        container_name = f"opencuria-workspace-{workspace_id}"
        network_name = f"opencuria-ws-{workspace_id}"

        try:
            client = docker_sdk.from_env()
            container = client.containers.get(container_name)
            container.reload()
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            network_info = networks.get(network_name, {})
            container_ip = network_info.get("IPAddress")
            if not container_ip:
                return None

            async_to_sync(self._connect_backend_to_workspace_network)(network_name)
            with socket.create_connection((container_ip, 6901), timeout=1):
                pass

            self._record_active_desktop(
                workspace_id,
                {
                    "port": 6901,
                    "container_ip": container_ip,
                    "network_name": network_name,
                },
                runner_id=str(workspace.runner_id),
            )
            return self._active_desktops.get(workspace_id)
        except Exception:
            logger.debug(
                "desktop_state_recovery_failed for workspace %s",
                workspace_id,
                exc_info=True,
            )
            return None

    def _record_active_desktop(
        self,
        workspace_id: str,
        desktop_state: dict,
        *,
        runner_id: str | None = None,
    ) -> None:
        """Persist backend desktop state without performing network I/O."""
        self._active_desktops[workspace_id] = dict(desktop_state)
        if runner_id:
            self._desktop_workspace_runner[workspace_id] = runner_id

    def _sync_desktop_state_from_heartbeat(
        self,
        workspace_id: str,
        desktop_state: dict | None,
        *,
        runner_id: str,
    ) -> None:
        """Reconcile cached desktop state from runner heartbeats."""
        if not desktop_state:
            self._cleanup_desktop_state(workspace_id)
            return

        current = self._active_desktops.get(workspace_id)
        current_runner = self._desktop_workspace_runner.get(workspace_id)
        if current == desktop_state and current_runner == runner_id:
            return

        self._cleanup_desktop_state(workspace_id)
        self._record_active_desktop(
            workspace_id,
            desktop_state,
            runner_id=runner_id,
        )

    def _cleanup_desktop_state(self, workspace_id: str) -> None:
        """Remove in-memory desktop state and detach the backend from its network."""
        desktop_info = self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)
        if not desktop_info:
            return

        network_name = desktop_info.get("network_name")
        if not network_name:
            return

        try:
            async_to_sync(self._disconnect_backend_from_workspace_network)(
                network_name
            )
        except Exception:
            logger.exception(
                "Failed to disconnect backend from workspace network %s",
                network_name,
            )

    @staticmethod
    def _backend_container_target() -> str:
        """Return the identifier used to attach the backend to Docker networks."""
        return (
            os.environ.get("OPENCURIA_BACKEND_CONTAINER_NAME")
            or os.environ.get("HOSTNAME")
            or "opencuria_local_backend"
        )

    @staticmethod
    async def _connect_backend_to_workspace_network(network_name: str) -> None:
        """Connect the backend container to a workspace's Docker network."""
        import docker as docker_sdk

        def _connect():
            client = docker_sdk.from_env()
            backend_name = RunnerService._backend_container_target()
            try:
                network = client.networks.get(network_name)
                network.connect(backend_name)
            except Exception as exc:
                err_str = str(exc).lower()
                if "already exists" in err_str or "endpoint with name" in err_str:
                    return  # already connected
                raise

        await asyncio.to_thread(_connect)

    @staticmethod
    async def _disconnect_backend_from_workspace_network(network_name: str) -> None:
        """Disconnect the backend container from a workspace's Docker network."""
        import docker as docker_sdk

        def _disconnect():
            client = docker_sdk.from_env()
            backend_name = RunnerService._backend_container_target()
            try:
                network = client.networks.get(network_name)
                network.disconnect(backend_name, force=True)
            except Exception:
                pass  # already disconnected or network gone

        await asyncio.to_thread(_disconnect)

    # ------------------------------------------------------------------
    # File explorer (stateless passthrough — no DB models)
    # ------------------------------------------------------------------

    async def forward_files_event(
        self,
        workspace_id: str,
        event: str,
        data: dict,
    ) -> None:
        """Forward a file explorer event from frontend to the runner.

        Looks up the workspace's runner and emits the event directly.
        When the runner is offline, a synthetic error result is sent back
        to the frontend so callers do not get stuck waiting indefinitely.
        """
        from .sio_server import emit_to_frontend

        # Map request event → result event for error responses.
        _result_event: dict[str, str] = {
            "files:read": "files:content_result",
            "files:list": "files:list_result",
            "files:upload": "files:upload_result",
            "files:download": "files:download_result",
        }

        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            result_event = _result_event.get(event)
            if result_event:
                error_payload: dict = {
                    "workspace_id": workspace_id,
                    "request_id": data.get("request_id", ""),
                    "path": data.get("path", ""),
                    "error": "Runner is offline",
                }
                if result_event == "files:content_result":
                    error_payload.update({"content": "", "size": 0, "truncated": False})
                elif result_event == "files:list_result":
                    error_payload["entries"] = []
                elif result_event == "files:upload_result":
                    error_payload["status"] = "error"
                await emit_to_frontend(result_event, error_payload, workspace_id)
            return

        await self._emit_to_runner(runner, event, data)
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    async def handle_files_result(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> None:
        """Forward a file result event from runner to subscribed frontends."""
        from .sio_server import emit_to_frontend

        workspace_id = data.get("workspace_id", "")
        if runner_id and workspace_id:
            try:
                workspace = await sync_to_async(self.workspaces.get_by_id)(
                    uuid.UUID(workspace_id)
                )
                if workspace is None or str(workspace.runner_id) != runner_id:
                    logger.warning(
                        "%s rejected: workspace %s does not belong to runner %s",
                        event,
                        workspace_id,
                        runner_id,
                    )
                    return
            except (ValueError, TypeError):
                logger.warning(
                    "%s rejected: invalid workspace_id %s", event, workspace_id
                )
                return

        await emit_to_frontend(event, data, workspace_id)

    def _get_workspace_auto_stop_deadline(self, workspace) -> datetime | None:
        """Return the inactivity deadline for a workspace, or None when disabled."""
        organization = getattr(getattr(workspace, "runner", None), "organization", None)
        timeout_minutes = getattr(
            organization,
            "workspace_auto_stop_timeout_minutes",
            None,
        )
        if (
            timeout_minutes is None
            or workspace.status != WorkspaceStatus.RUNNING
            or workspace.last_activity_at is None
        ):
            return None
        return workspace.last_activity_at + timedelta(minutes=timeout_minutes)

    def _should_auto_stop_workspace(
        self,
        workspace,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return True if inactivity policy requires stopping the workspace."""
        if workspace.status != WorkspaceStatus.RUNNING:
            return False
        if workspace.active_operation:
            return False
        if bool(getattr(workspace, "has_active_session", False)):
            return False
        runner = getattr(workspace, "runner", None)
        if runner is None or not runner.is_online:
            return False
        deadline = self._get_workspace_auto_stop_deadline(workspace)
        if deadline is None:
            return False
        return deadline <= (now or timezone.now())

    async def auto_stop_inactive_workspaces(
        self,
        *,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> list["Task"]:
        """Stop running workspaces whose inactivity deadline has elapsed."""
        evaluation_time = now or timezone.now()
        if runner_id is not None:
            workspaces = await sync_to_async(
                lambda: list(self.workspaces.list_by_runner(runner_id))
            )()
        elif organization_id is not None:
            workspaces = await sync_to_async(
                lambda: list(self.workspaces.list_by_organization(organization_id))
            )()
        else:
            workspaces = await sync_to_async(lambda: list(self.workspaces.list_all()))()

        dispatched: list["Task"] = []
        for workspace in workspaces:
            if not self._should_auto_stop_workspace(workspace, now=evaluation_time):
                continue
            try:
                task = await self.stop_workspace(workspace.id)
            except (ConflictError, RunnerOfflineError, WorkspaceStateError) as exc:
                logger.info(
                    "Skipped auto-stop for workspace %s: %s",
                    workspace.id,
                    exc,
                )
                continue
            logger.info(
                "Dispatched inactivity auto-stop for workspace %s",
                workspace.id,
            )
            dispatched.append(task)

        return dispatched

    def handle_heartbeat(
        self,
        runner: "Runner",
        workspaces: list[dict],
    ) -> None:
        """Handle runner:heartbeat event — reconcile workspace state.

        Compares the runner's reported container states with the backend's
        records and updates any stale entries.

        Args:
            runner: The Runner that sent the heartbeat.
            workspaces: List of dicts with workspace_id, status, agent_type.
        """
        from django.utils import timezone as tz

        # Update heartbeat timestamp
        self.runners.update_heartbeat(runner)

        # Build lookup of runner-reported workspace states
        runner_ws_payloads: dict[str, dict] = {}
        runner_ws_states: dict[str, str] = {}
        for ws_data in workspaces:
            ws_id = ws_data.get("workspace_id", "")
            status = ws_data.get("status", "unknown")
            runner_ws_payloads[ws_id] = ws_data
            runner_ws_states[ws_id] = status

        # Check backend workspaces for this runner
        backend_workspaces = list(
            self.workspaces.list_by_runner(runner.id)
        )
        backend_workspace_ids = {str(ws.id) for ws in backend_workspaces}
        runner_id_str = str(runner.id)

        # Drop stale pending cleanup entries that are no longer present on the
        # runner heartbeat.
        active_pending = {
            key
            for key in self._pending_unknown_workspace_cleanup
            if key[0] == runner_id_str
        }
        for key in active_pending:
            if key[1] not in runner_ws_states:
                self._pending_unknown_workspace_cleanup.discard(key)

        # Runner reported instances that backend does not know: request cleanup.
        unknown_workspace_ids = sorted(
            set(runner_ws_states.keys()) - backend_workspace_ids
        )
        for unknown_workspace_id in unknown_workspace_ids:
            self._request_workspace_cleanup(
                runner,
                workspace_id=unknown_workspace_id,
                reason="unknown_runtime_workspace",
            )

        for ws in backend_workspaces:
            ws_id_str = str(ws.id)
            cleanup_key = (runner_id_str, ws_id_str)
            runner_status = runner_ws_states.get(ws_id_str)
            runner_payload = runner_ws_payloads.get(ws_id_str, {})

            if ws.status in (
                WorkspaceStatus.FAILED,
                WorkspaceStatus.REMOVED,
            ):
                if runner_status is not None:
                    self._request_workspace_cleanup(
                        runner,
                        workspace_id=ws_id_str,
                        reason=f"backend_terminal_state:{ws.status}",
                    )
                else:
                    self._pending_unknown_workspace_cleanup.discard(cleanup_key)
                continue

            # This workspace is backend-managed and non-terminal.
            self._pending_unknown_workspace_cleanup.discard(cleanup_key)

            if runner_status is None:
                # Workspace exists in backend but not on runner —
                # container was removed externally.
                if ws.status in (
                    WorkspaceStatus.RUNNING,
                    WorkspaceStatus.STOPPED,
                ):
                    logger.warning(
                        "Workspace %s missing from runner %s, marking failed",
                        ws_id_str,
                        runner.id,
                    )
                    self.workspaces.update_status(
                        ws, WorkspaceStatus.FAILED
                    )
                    self._forward_to_frontend(
                        "workspace:status_changed",
                        {
                            "workspace_id": ws_id_str,
                            "status": "failed",
                        },
                        ws_id_str,
                    )
                self._cleanup_desktop_state(ws_id_str)
            else:
                # Map Docker container status to workspace status
                new_status = self._map_instance_status(runner_status)
                if new_status and new_status != ws.status:
                    # Never promote CREATING → RUNNING via heartbeat.
                    # Only the explicit workspace:created event (sent after
                    # repos are cloned and SSH is established) may do that.
                    if (
                        ws.status == WorkspaceStatus.CREATING
                        and new_status == WorkspaceStatus.RUNNING
                    ):
                        continue
                    logger.info(
                        "Heartbeat: workspace %s status %s -> %s",
                        ws_id_str,
                        ws.status,
                        new_status,
                    )
                    self.workspaces.update_status(ws, new_status)
                    self._forward_to_frontend(
                        "workspace:status_changed",
                        {
                            "workspace_id": ws_id_str,
                            "status": new_status,
                        },
                        ws_id_str,
                    )

                if not (
                    new_status == WorkspaceStatus.RUNNING
                    or (new_status is None and ws.status == WorkspaceStatus.RUNNING)
                ):
                    self._cleanup_desktop_state(ws_id_str)
                    continue

                if "desktop" in runner_payload:
                    self._sync_desktop_state_from_heartbeat(
                        ws_id_str,
                        runner_payload.get("desktop"),
                        runner_id=runner_id_str,
                    )

        async_to_sync(self.auto_stop_inactive_workspaces)(runner_id=runner.id)

    def handle_unknown_workspace_cleanup_result(
        self,
        runner: "Runner",
        workspace_id: str,
        *,
        cleaned: bool,
        error: str | None = None,
    ) -> None:
        """Handle result events for unknown-workspace cleanup requests."""
        cleanup_key = (str(runner.id), workspace_id)
        self._pending_unknown_workspace_cleanup.discard(cleanup_key)

        if error:
            logger.error(
                "Unknown workspace cleanup failed on runner %s (workspace=%s): %s",
                runner.id,
                workspace_id,
                error,
            )
            return

        logger.info(
            "Unknown workspace cleanup completed on runner %s (workspace=%s, cleaned=%s)",
            runner.id,
            workspace_id,
            cleaned,
        )

    def _request_workspace_cleanup(
        self,
        runner: "Runner",
        *,
        workspace_id: str,
        reason: str,
    ) -> None:
        """Request runner cleanup for a runtime workspace ID, deduplicated."""
        cleanup_key = (str(runner.id), workspace_id)
        if cleanup_key in self._pending_unknown_workspace_cleanup:
            return

        logger.warning(
            "Heartbeat: requesting cleanup for workspace %s on runner %s (%s)",
            workspace_id,
            runner.id,
            reason,
        )
        self._pending_unknown_workspace_cleanup.add(cleanup_key)
        async_to_sync(self._emit_to_runner)(
            runner,
            "task:cleanup_unknown_workspace",
            {"workspace_id": workspace_id},
        )

    @staticmethod
    def _map_instance_status(status: str) -> str | None:
        """Map a runtime instance status string to a WorkspaceStatus value.

        Supports both Docker container states and QEMU/libvirt domain states.
        """
        mapping = {
            # Docker states
            "running": WorkspaceStatus.RUNNING,
            "exited": WorkspaceStatus.STOPPED,
            "dead": WorkspaceStatus.FAILED,
            "removing": WorkspaceStatus.REMOVED,
            "created": WorkspaceStatus.CREATING,
            # QEMU/libvirt states
            "stopped": WorkspaceStatus.STOPPED,
            "failed": WorkspaceStatus.FAILED,
            "removed": WorkspaceStatus.REMOVED,
        }
        return mapping.get(status)

    # ------------------------------------------------------------------
    # Query methods (used by REST API)
    # ------------------------------------------------------------------

    def list_runners(self, organization_id: uuid.UUID | None = None) -> list["Runner"]:
        """Return all registered runners, optionally filtered by organization."""
        if organization_id:
            return list(self.runners.list_by_organization(organization_id))
        return list(self.runners.list_all())

    def get_runner(self, runner_id: uuid.UUID) -> "Runner":
        """Return a runner by ID or raise RunnerNotFoundError."""
        runner = self.runners.get_by_id(runner_id)
        if runner is None:
            raise RunnerNotFoundError(str(runner_id))
        return runner

    def update_runner_qemu_settings(
        self,
        runner_id: uuid.UUID,
        **fields,
    ) -> "Runner":
        """Update per-runner QEMU resource limits/defaults."""
        runner = self.get_runner(runner_id)
        updated_fields = dict(fields)
        if not updated_fields:
            return runner
        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=RuntimeType.QEMU,
        )
        for key, value in updated_fields.items():
            setattr(runner, key, value)
        self._validate_runner_qemu_limits(runner)
        return self.runners.update_qemu_settings(runner, **updated_fields)

    def list_workspaces(
        self,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        user=None,
        is_admin: bool = False,
    ) -> list["Workspace"]:
        """Return workspaces filtered by org/user access."""
        if runner_id:
            qs = self.workspaces.list_by_runner(runner_id)
        elif organization_id:
            qs = self.workspaces.list_by_organization(organization_id)
        else:
            qs = self.workspaces.list_all()

        # Non-admins only see their own workspaces
        if user and not is_admin:
            qs = qs.filter(created_by=user)

        return list(qs)

    def get_workspace(self, workspace_id: uuid.UUID) -> "Workspace":
        """Return a workspace by ID or raise WorkspaceNotFoundError."""
        workspace = self.workspaces.get_by_id(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        return workspace

    def list_sessions(self, workspace_id: uuid.UUID) -> list["Session"]:
        """Return all sessions for a workspace."""
        return list(self.sessions.list_by_workspace(workspace_id))

    def list_chats(self, workspace_id: uuid.UUID) -> list:
        """Return all chats for a workspace."""
        return list(self.chats.list_by_workspace(workspace_id))

    def list_chat_sessions(self, chat_id: uuid.UUID) -> list["Session"]:
        """Return all sessions for a specific chat."""
        return list(self.sessions.list_by_chat(chat_id))

    def create_chat(
        self,
        workspace_id: uuid.UUID,
        name: str = "",
        agent_definition_id: uuid.UUID | None = None,
    ) -> "Chat":
        """Create a new chat within a workspace."""
        workspace = self.workspaces.get_by_id(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        # Validate agent exists if specified
        agent_def = None
        agent_type = ""
        if agent_definition_id:
            agent_def = self.agents.get_visible_by_id(
                agent_definition_id,
                workspace.runner.organization_id,
            )
            if agent_def is None:
                raise ValueError(f"Unknown agent definition: '{agent_definition_id}'")
            agent_type = agent_def.name
            if agent_def.required_credential_services.exists():
                required_slugs = self._get_agent_required_credential_service_slugs(agent_def)
                workspace_slugs = self._get_workspace_credential_service_slugs(workspace)
                missing_slugs = [slug for slug in required_slugs if slug not in workspace_slugs]
                if missing_slugs:
                    raise ConflictError(
                        "Workspace is missing required credentials for agent "
                        f"'{agent_def.name}': {', '.join(missing_slugs)}"
                    )

        return self.chats.create(
            chat_id=generate_uuid(),
            workspace=workspace,
            name=name,
            agent_definition=agent_def,
            agent_type=agent_type,
        )

    def rename_chat(self, chat_id: uuid.UUID, name: str) -> "Chat":
        """Rename an existing chat."""
        chat = self.chats.get_by_id(chat_id)
        if chat is None:
            raise ValueError(f"Chat '{chat_id}' not found")
        trimmed = name.strip()
        if not trimmed:
            raise ValueError("Chat name must not be empty")
        return self.chats.update_name(chat, trimmed)

    def delete_chat(self, chat_id: uuid.UUID) -> None:
        """Delete a chat and its sessions."""
        chat = self.chats.get_by_id(chat_id)
        if chat is None:
            raise ValueError(f"Chat '{chat_id}' not found")
        self.chats.delete(chat_id)

    def mark_conversation_read(
        self,
        session_id: uuid.UUID,
    ) -> None:
        """
        Mark a session as read.

        Sets ``read_at`` if the session was previously COMPLETED or FAILED, so
        that ``ConversationRepository.list_for_user`` reflects ``is_read=True``
        for the corresponding conversation without overwriting the outcome.
        """
        session = self.sessions.get_by_id(session_id)
        if session is not None:
            self.sessions.mark_read(session)

    def mark_conversation_unread(
        self,
        session_id: uuid.UUID,
    ) -> None:
        """
        Mark a session as unread again.

        Clears ``read_at`` for completed or failed sessions so the frontend can
        explicitly resurface a reply as unread until the user re-enters the
        conversation.
        """
        session = self.sessions.get_by_id(session_id)
        if session is not None:
            self.sessions.mark_unread(session)

    def get_available_agents(
        self,
        organization_id: uuid.UUID | None = None,
        user=None,
        workspace=None,
    ) -> list[dict]:
        """Return agent definitions from the DB with availability metadata.

        When ``workspace`` is provided, availability is calculated against
        the credentials already attached to the workspace. Otherwise, credential
        availability falls back to credentials visible to the current user
        within the organization.

        Note: Agents are independent of runners. All agents are available
        as long as there is at least one online runner in the organization.
        """
        all_agents = list(self.agents.list_all_with_credential_slugs(organization_id=organization_id))

        # Check online runner availability (generic, no agent-specific support).
        if workspace is not None:
            has_online_runner = workspace.runner.is_online
        elif organization_id:
            has_online_runner = self.runners.list_by_organization(organization_id).filter(
                status=RunnerStatus.ONLINE
            ).exists()
        else:
            has_online_runner = self.runners.list_online().exists()

        if workspace is not None:
            available_credential_slugs = self._get_workspace_credential_service_slugs(
                workspace
            )
        else:
            # Gather credential service slugs the user already has credentials for
            available_credential_slugs: set[str] = set()
            if user and organization_id:
                from apps.credentials.models import Credential

                user_creds = Credential.objects.filter(
                    user=user,
                ).select_related("service")
                org_creds = Credential.objects.filter(
                    organization__id=organization_id,
                ).select_related("service")
                for cred in list(user_creds) + list(org_creds):
                    if cred.service.slug:
                        available_credential_slugs.add(cred.service.slug)

        result = []
        for agent in all_agents:
            required_slugs = self._get_agent_required_credential_service_slugs(agent)
            has_credentials = all(
                slug in available_credential_slugs for slug in required_slugs
            )
            result.append(
                {
                    "id": str(agent.id),
                    "name": agent.name,
                    "description": agent.description,
                    "available_options": list(agent.available_options or []),
                    "supports_multi_chat": agent.supports_multi_chat,
                    "has_online_runner": has_online_runner,
                    "required_credential_service_slugs": required_slugs,
                    "has_credentials": has_credentials,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _emit_to_runner(
        self,
        runner: "Runner",
        event: str,
        data: dict,
    ) -> None:
        """Send a Socket.IO event to a specific runner by its SID."""
        if self.sio is None:
            logger.error("No Socket.IO server configured — cannot emit events")
            return

        if not runner.sid:
            logger.error(
                "Runner %s has no SID — cannot send event %s",
                runner.id,
                event,
            )
            return

        await self.sio.emit(event, data, to=runner.sid)
        logger.debug("Emitted %s to runner %s: %s", event, runner.id, data)

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
        from .sio_server import emit_to_frontend

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                async_to_sync(emit_to_frontend)(event, data, workspace_id)
            else:
                loop.create_task(emit_to_frontend(event, data, workspace_id))
        except Exception:
            logger.exception(
                "Failed forwarding event to frontend",
                extra={
                    "event": event,
                    "workspace_id": workspace_id,
                },
            )

    # ------------------------------------------------------------------
    # Image definition operations
    # ------------------------------------------------------------------

    @staticmethod
    def _build_package_install_block(base_distro: str, packages: list[str]) -> str:
        """Generate package installation block based on distro family."""
        clean_packages = [p.strip() for p in packages if p.strip()]
        if not clean_packages:
            return ""

        distro = (base_distro or "").lower()
        if "alpine" in distro:
            return "RUN apk add --no-cache " + " ".join(clean_packages)

        # Default to apt for ubuntu/debian and unknown distros.
        return (
            "RUN apt-get update && apt-get install -y \\\n"
            f"    {' '.join(clean_packages)} \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )

    @staticmethod
    def _validate_qemu_base_distro(base_distro: str) -> None:
        """Ensure QEMU image definitions use a supported distro source."""
        distro = (base_distro or "").strip().lower()
        if distro.startswith("ubuntu:"):
            return
        raise ConflictError(
            "QEMU image definitions currently require an ubuntu:<version> base distro"
        )

    @staticmethod
    def _desktop_session_dockerfile_block() -> str:
        """Return Dockerfile lines that install KasmVNC desktop session support."""
        return """# --- KasmVNC desktop session support ---
RUN apt-get update && apt-get install -y \\
    xfonts-base openbox dbus-x11 x11-xserver-utils \\
    libnss3 libatk-bridge2.0-0 libcups2 libdrm2 \\
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \\
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \\
    wget ca-certificates \\
    && (apt-get install -y libasound2t64 || apt-get install -y libasound2) \\
    && wget -q -O /tmp/kasmvnc.deb \\
       "https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_jammy_1.3.3_amd64.deb" \\
    && apt-get install -y /tmp/kasmvnc.deb || true \\
    && apt-get install -f -y \\
    && rm -f /tmp/kasmvnc.deb \\
    && wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \\
    && (apt-get install -y /tmp/google-chrome.deb || apt-get install -f -y) \\
    && rm -f /tmp/google-chrome.deb \\
    && rm -rf /var/lib/apt/lists/*

# Pre-configure KasmVNC (skip interactive wizard)
RUN mkdir -p /root/.vnc \\
    && touch /root/.vnc/.de-was-selected \\
    && printf "password\\npassword\\n" | vncpasswd -u root -w -r 2>/dev/null || true \\
    && printf 'desktop:\\n  resolution:\\n    width: 1920\\n    height: 1080\\n  allow_resize: true\\nnetwork:\\n  protocol: http\\n  interface: 0.0.0.0\\n  websocket_port: 6901\\n  ssl:\\n    require_ssl: false\\n    pem_certificate:\\n    pem_key:\\n' > /root/.vnc/kasmvnc.yaml \\
    && printf '#!/bin/bash\\nset -eu\\nfor browser in google-chrome-stable google-chrome chromium chromium-browser /usr/lib/chromium/chromium; do\\n  if [ \"${browser#/}\" != \"$browser\" ]; then\\n    if [ -x \"$browser\" ]; then\\n      exec \"$browser\" --no-sandbox --disable-gpu --start-maximized --disable-dev-shm-usage --no-first-run\\n    fi\\n    continue\\n  fi\\n  if command -v \"$browser\" >/dev/null 2>&1; then\\n    if [ \"$browser\" = \"chromium-browser\" ] && ! chromium-browser --version >/dev/null 2>&1; then\\n      continue\\n    fi\\n    exec \"$browser\" --no-sandbox --disable-gpu --start-maximized --disable-dev-shm-usage --no-first-run\\n  fi\\ndone\\necho \"No supported browser binary found for desktop session\" >&2\\n' > /usr/local/bin/opencuria-desktop-browser \\
    && printf '#!/bin/bash\\nexport DISPLAY=:1\\nexport HOME=/root\\nopenbox-session &\\nsleep 1\\n/usr/local/bin/opencuria-desktop-browser >/root/.vnc/browser.log 2>&1 &\\nwait\\n' > /root/.vnc/xstartup \\
    && chmod +x /root/.vnc/xstartup /usr/local/bin/opencuria-desktop-browser

# Desktop start/stop scripts (use Xvnc directly to avoid KasmVNC perl wrapper prompts)
RUN printf '#!/bin/bash\\nset -e\\nexport DISPLAY=:1\\nexport HOME=/root\\n/usr/local/bin/opencuria-desktop-stop 2>/dev/null || true\\nmkdir -p /root/.vnc\\nrm -f /tmp/.X1-lock /tmp/.X11-unix/X1\\n/usr/bin/Xvnc :1 -geometry 1920x1080 -depth 24 -rfbport 5901 -SecurityTypes None -disableBasicAuth -websocketPort 6901 -httpd /usr/share/kasmvnc/www -interface 0.0.0.0 -AlwaysShared -AcceptKeyEvents -AcceptPointerEvents -AcceptSetDesktopSize -SendCutText -AcceptCutText >>/root/.vnc/server.log 2>&1 &\\nfor _ in $(seq 1 120); do\\n  if [ -e /tmp/.X11-unix/X1 ]; then\\n    /root/.vnc/xstartup >>/root/.vnc/xstartup.log 2>&1 &\\n    echo \"Desktop session started on :1 (ws port 6901)\"\\n    exit 0\\n  fi\\n  sleep 0.25\\ndone\\necho \"Desktop session failed to start\" >&2\\nexit 1\\n' > /usr/local/bin/opencuria-desktop-start \
    && printf '#!/bin/bash\\nfor pid in $(pgrep -f "Xvnc.*:1" 2>/dev/null); do kill "$pid" 2>/dev/null || true; done\\nfor pid in $(pgrep -f "openbox" 2>/dev/null); do kill "$pid" 2>/dev/null || true; done\\nrm -f /tmp/.X1-lock /tmp/.X11-unix/X1\\n' > /usr/local/bin/opencuria-desktop-stop \
    && chmod +x /usr/local/bin/opencuria-desktop-start /usr/local/bin/opencuria-desktop-stop
"""

    @staticmethod
    def _desktop_session_init_script_block() -> str:
        """Return shell script lines that install KasmVNC in a QEMU init script."""
        return """
# --- KasmVNC desktop session support ---
apt-get update
apt-get install -y xfonts-base openbox dbus-x11 x11-xserver-utils \\
    libnss3 libatk-bridge2.0-0 libcups2 libdrm2 \\
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \\
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 wget ca-certificates
(apt-get install -y libasound2t64 || apt-get install -y libasound2)

wget -q -O /tmp/kasmvnc.deb \\
    "https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_jammy_1.3.3_amd64.deb"
apt-get install -y /tmp/kasmvnc.deb || true
apt-get install -f -y
rm -f /tmp/kasmvnc.deb

wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y /tmp/google-chrome.deb || apt-get install -f -y
rm -f /tmp/google-chrome.deb

# Pre-configure KasmVNC
mkdir -p /root/.vnc
touch /root/.vnc/.de-was-selected
printf "password\\npassword\\n" | vncpasswd -u root -w -r 2>/dev/null || true

cat >/root/.vnc/kasmvnc.yaml <<'KASMCFG'
desktop:
  resolution:
    width: 1920
    height: 1080
  allow_resize: true
network:
  protocol: http
  interface: 0.0.0.0
  websocket_port: 6901
  ssl:
    require_ssl: false
    pem_certificate:
    pem_key:
KASMCFG

cat >/usr/local/bin/opencuria-desktop-browser <<'BROWSER'
#!/bin/bash
set -eu
for browser in google-chrome-stable google-chrome chromium chromium-browser /usr/lib/chromium/chromium; do
  if [ "${browser#/}" != "$browser" ]; then
    if [ -x "$browser" ]; then
      exec "$browser" --no-sandbox --disable-gpu --start-maximized --disable-dev-shm-usage --no-first-run
    fi
    continue
  fi
  if command -v "$browser" >/dev/null 2>&1; then
    if [ "$browser" = "chromium-browser" ] && ! chromium-browser --version >/dev/null 2>&1; then
      continue
    fi
    exec "$browser" --no-sandbox --disable-gpu --start-maximized --disable-dev-shm-usage --no-first-run
  fi
done
echo "No supported browser binary found for desktop session" >&2
BROWSER

cat >/root/.vnc/xstartup <<'XSTARTUP'
#!/bin/bash
export DISPLAY=:1
export HOME=/root
openbox-session &
sleep 1
/usr/local/bin/opencuria-desktop-browser >/root/.vnc/browser.log 2>&1 &
wait
XSTARTUP
chmod +x /root/.vnc/xstartup
chmod +x /usr/local/bin/opencuria-desktop-browser

cat >/usr/local/bin/opencuria-desktop-start <<'DESKSTART'
#!/bin/bash
set -e
export DISPLAY=:1
export HOME=/root
/usr/local/bin/opencuria-desktop-stop 2>/dev/null || true
mkdir -p /root/.vnc
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1

# Launch Xvnc directly (bypasses KasmVNC perl wrapper which prompts for user input)
/usr/bin/Xvnc :1 \
    -geometry 1920x1080 \
    -depth 24 \
    -rfbport 5901 \
    -SecurityTypes None \
    -disableBasicAuth \
    -websocketPort 6901 \
    -httpd /usr/share/kasmvnc/www \
    -interface 0.0.0.0 \
    -AlwaysShared \
    -AcceptKeyEvents \
    -AcceptPointerEvents \
    -AcceptSetDesktopSize \
    -SendCutText \
    -AcceptCutText \
    >>/root/.vnc/server.log 2>&1 &

for _ in $(seq 1 120); do
  if [ -e /tmp/.X11-unix/X1 ]; then
    # Start the window manager and browser via xstartup
    /root/.vnc/xstartup >>/root/.vnc/xstartup.log 2>&1 &
    echo "Desktop session started on :1 (ws port 6901)"
    exit 0
  fi
  sleep 0.25
done
echo "Desktop session failed to start" >&2
exit 1
DESKSTART

cat >/usr/local/bin/opencuria-desktop-stop <<'DESKSTOP'
#!/bin/bash
# Stop Xvnc and all desktop processes
for pid in $(pgrep -f 'Xvnc.*:1' 2>/dev/null); do
    kill "$pid" 2>/dev/null || true
done
for pid in $(pgrep -f 'openbox' 2>/dev/null); do
    kill "$pid" 2>/dev/null || true
done
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
DESKSTOP

chmod +x /usr/local/bin/opencuria-desktop-start /usr/local/bin/opencuria-desktop-stop
rm -rf /var/lib/apt/lists/*
"""

    @classmethod
    def _build_qemu_init_script_content(cls, definition) -> str:
        """Build a shell init script for QEMU image definitions."""
        lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            "",
        ]

        packages = [p.strip() for p in list(definition.packages or []) if p.strip()]
        distro = (definition.base_distro or "").lower()
        if packages:
            if "alpine" in distro:
                lines += [
                    f"apk add --no-cache {' '.join(packages)}",
                    "",
                ]
            else:
                lines += [
                    "export DEBIAN_FRONTEND=noninteractive",
                    "apt-get update",
                    f"apt-get install -y {' '.join(packages)}",
                    "rm -rf /var/lib/apt/lists/*",
                    "",
                ]

        env_vars = dict(definition.env_vars or {})
        if env_vars:
            lines += [
                "cat >/etc/profile.d/opencuria-image-env.sh <<'EOF'",
                "#!/bin/sh",
            ]
            for key, value in env_vars.items():
                if key:
                    escaped = str(value).replace('"', '\\"')
                    lines.append(f'export {key}="{escaped}"')
            lines += [
                "EOF",
                "chmod 644 /etc/profile.d/opencuria-image-env.sh",
                "",
            ]

        custom_script = (definition.custom_init_script or "").strip()
        if custom_script:
            lines += [
                "# Custom image definition steps",
                custom_script,
                "",
            ]

        # Always include KasmVNC desktop session support (non-Alpine only)
        distro_check = (definition.base_distro or "").lower()
        if "alpine" not in distro_check:
            lines += [cls._desktop_session_init_script_block(), ""]

        return "\n".join(lines).strip() + "\n"

    @classmethod
    def _generate_dockerfile_content(cls, definition) -> str:
        """Build Dockerfile content from an image definition record."""
        lines = [f"FROM {definition.base_distro}", ""]

        if "alpine" not in (definition.base_distro or "").lower():
            lines += ["ENV DEBIAN_FRONTEND=noninteractive", ""]

        install_block = cls._build_package_install_block(
            definition.base_distro, list(definition.packages or [])
        )
        if install_block:
            lines += [install_block, ""]

        for key, value in dict(definition.env_vars or {}).items():
            if key:
                lines.append(f"ENV {key}={value}")
        if definition.env_vars:
            lines.append("")

        if definition.custom_dockerfile:
            lines += [definition.custom_dockerfile.strip(), ""]

        # Always include KasmVNC desktop session support (non-Alpine only)
        if "alpine" not in (definition.base_distro or "").lower():
            lines += [cls._desktop_session_dockerfile_block(), ""]

        lines += [
            'CMD ["tail", "-f", "/dev/null"]',
        ]
        return "\n".join(lines).strip() + "\n"

    def list_image_definitions(self, organization_id: uuid.UUID) -> list:
        """List image definitions for an organization."""
        return list(self.image_definitions.list_by_org(organization_id))

    def list_runner_image_builds(
        self,
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> list:
        """List runner build records for an image definition."""
        return list(
            self.runner_image_builds.list_for_definition(
                image_definition_id,
                organization_id=organization_id,
            )
        )

    async def trigger_runner_image_build(
        self,
        *,
        image_definition,
        runner,
        activate: bool = True,
        created_by=None,
    ):
        """Create/update runner build record and dispatch task:build_image."""
        from .models import ImageArtifact, RunnerImageBuild

        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=image_definition.runtime_type,
        )

        existing = await sync_to_async(self.runner_image_builds.get)(
            image_definition.id, runner.id
        )
        if existing is None:
            build = await sync_to_async(RunnerImageBuild.objects.create)(
                image_definition=image_definition,
                runner=runner,
                status=RunnerImageBuild.Status.PENDING,
            )
        else:
            build = existing
            build.status = RunnerImageBuild.Status.PENDING
            if not activate:
                build.status = RunnerImageBuild.Status.DEACTIVATED
            await sync_to_async(build.save)(update_fields=["status", "updated_at"])

        if not activate:
            existing_artifact = await sync_to_async(
                self.image_artifacts.get_by_runner_image_build_id
            )(build.id)
            if existing_artifact is not None:
                await sync_to_async(self.image_artifacts.mark_failed)(existing_artifact.id)
            return build

        if image_definition.runtime_type == RuntimeType.QEMU:
            self._validate_qemu_base_distro(image_definition.base_distro)

        task = await sync_to_async(self.tasks.create)(
            task_id=generate_uuid(),
            runner=runner,
            task_type=TaskType.BUILD_IMAGE,
        )
        await sync_to_async(
            RunnerImageBuild.objects.filter(id=build.id).update
        )(
            build_task=task,
            status=RunnerImageBuild.Status.PENDING,
            image_tag=(
                f"opencuria/custom/{re.sub(r'[^a-z0-9-]+', '-', image_definition.name.lower())}:{build.id}"
                if image_definition.runtime_type == RuntimeType.DOCKER
                else ""
            ),
            image_path=(
                f"/var/lib/opencuria/base-images/{build.id}.qcow2"
                if image_definition.runtime_type == RuntimeType.QEMU
                else ""
            ),
        )
        artifact = await sync_to_async(
            self.image_artifacts.get_by_runner_image_build_id
        )(build.id)
        artifact_name = f"{image_definition.name} ({runner.name})"
        if artifact is None:
            await sync_to_async(self.image_artifacts.create_pending)(
                source_workspace=None,
                name=artifact_name,
                creating_task_id=str(task.id),
                artifact_kind=ImageArtifact.ArtifactKind.BUILT,
                runner_image_build=build,
                created_by=created_by,
            )
        else:
            artifact.name = artifact_name
            artifact.status = ImageArtifact.ArtifactStatus.CREATING
            artifact.created_by = created_by
            artifact.creating_task_id = str(task.id)
            artifact.runner_artifact_id = ""
            artifact.size_bytes = 0
            await sync_to_async(artifact.save)(
                update_fields=[
                    "name",
                    "status",
                    "created_by",
                    "creating_task_id",
                    "runner_artifact_id",
                    "size_bytes",
                ]
            )

        build = await sync_to_async(RunnerImageBuild.objects.select_related(
            "image_definition", "runner", "build_task"
        ).get)(id=build.id)

        payload = {
            "task_id": str(task.id),
            "runner_image_build_id": str(build.id),
            "runtime_type": image_definition.runtime_type,
        }
        if image_definition.runtime_type == RuntimeType.DOCKER:
            payload["dockerfile_content"] = self._generate_dockerfile_content(
                image_definition
            )
            payload["image_tag"] = build.image_tag
        else:
            payload["base_distro"] = image_definition.base_distro
            payload["init_script"] = self._build_qemu_init_script_content(
                image_definition
            )
            payload["image_path"] = build.image_path

        await self._emit_to_runner(runner, "task:build_image", payload)
        await sync_to_async(self.tasks.mark_in_progress)(task)
        return build

    def handle_image_build_progress(
        self, runner_image_build_id: str, line: str, runner_id: str | None = None
    ) -> None:
        """Append build log lines for runner image builds."""
        from .models import RunnerImageBuild

        try:
            build = RunnerImageBuild.objects.select_related("runner").get(
                id=runner_image_build_id
            )
        except RunnerImageBuild.DoesNotExist:
            return
        if runner_id and str(build.runner_id) != str(runner_id):
            return
        build.status = RunnerImageBuild.Status.BUILDING
        build.build_log = (build.build_log or "") + (line.rstrip("\n") + "\n")
        build.save(update_fields=["status", "build_log", "updated_at"])

    def handle_image_built(
        self,
        *,
        task_id: str,
        runner_image_build_id: str,
        image_tag: str = "",
        image_path: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Mark a runner image build as active and complete its task."""
        from django.utils import timezone
        from .models import ImageArtifact, RunnerImageBuild

        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        if not self._validate_task_runner(task, runner_id):
            return

        build = RunnerImageBuild.objects.get(id=runner_image_build_id)
        build.status = RunnerImageBuild.Status.ACTIVE
        if image_tag:
            build.image_tag = image_tag
        if image_path:
            build.image_path = image_path
        build.built_at = timezone.now()
        build.save(
            update_fields=["status", "image_tag", "image_path", "built_at", "updated_at"]
        )
        artifact = self.image_artifacts.get_by_runner_image_build_id(uuid.UUID(runner_image_build_id))
        runner_artifact_id = image_tag or image_path
        artifact_name = f"{build.image_definition.name} ({build.runner.name})"
        if artifact is None:
            self.image_artifacts.create(
                source_workspace=None,
                runner_artifact_id=runner_artifact_id,
                name=artifact_name,
                size_bytes=0,
                artifact_kind=ImageArtifact.ArtifactKind.BUILT,
                runner_image_build=build,
            )
        else:
            artifact.name = artifact_name
            artifact.status = ImageArtifact.ArtifactStatus.READY
            artifact.runner_artifact_id = runner_artifact_id
            artifact.size_bytes = 0
            artifact.creating_task_id = None
            artifact.save(
                update_fields=[
                    "name",
                    "status",
                    "runner_artifact_id",
                    "size_bytes",
                    "creating_task_id",
                ]
            )
        self.tasks.complete(task)

    def handle_image_build_failed(
        self,
        *,
        task_id: str,
        runner_image_build_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Mark a runner image build as failed and fail the correlated task."""
        from .models import RunnerImageBuild

        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        RunnerImageBuild.objects.filter(id=runner_image_build_id).update(
            status=RunnerImageBuild.Status.FAILED
        )
        artifact = self.image_artifacts.get_by_runner_image_build_id(
            uuid.UUID(runner_image_build_id)
        )
        if artifact is not None:
            self.image_artifacts.mark_failed(artifact.id)
        if task is not None:
            self.tasks.fail(task, error)

    # ------------------------------------------------------------------
    # Image artifact operations
    # ------------------------------------------------------------------

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str,
        organization_id: uuid.UUID | None = None,
    ) -> tuple["ImageArtifact", "Task"]:
        """Dispatch image artifact creation to the runner.

        Creates a 'creating' artifact record immediately so the UI can show
        progress. The record is updated to 'ready' when the runner completes.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        if organization_id and workspace.runner.organization_id != organization_id:
            raise WorkspaceNotFoundError(str(workspace_id))
        self._ensure_workspace_available(workspace)

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        # Verify runtime supports image artifact capture
        if workspace.runtime_type not in (runner.available_runtimes or []):
            raise ValueError(
                f"Runner does not support runtime '{workspace.runtime_type}'"
            )

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.CREATE_IMAGE_ARTIFACT,
            workspace=workspace,
        )

        # Capture credentials and create the artifact record upfront so the UI
        # can immediately show the 'creating' state.
        # Both .credentials and .created_by are fetched eagerly via get_by_id.
        workspace_credentials = await sync_to_async(
            lambda: list(workspace.credentials.all())
        )()
        created_by = await sync_to_async(lambda: workspace.created_by)()
        artifact = await sync_to_async(self.image_artifacts.create_pending)(
            source_workspace=workspace,
            name=name,
            creating_task_id=str(task_id),
            created_by=created_by,
            credentials=workspace_credentials,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:create_image_artifact",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.CREATE_IMAGE_ARTIFACT),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "name": name,
            },
        )
        logger.info(
            "Dispatched create_image_artifact (workspace=%s, task=%s, artifact=%s)",
            workspace_id,
            task_id,
            artifact.id,
        )
        return workspace, task

    def handle_image_artifact_created(
        self,
        task_id: str,
        workspace_id: str,
        artifact_id: str,
        name: str,
        size_bytes: int = 0,
        runner_id: str | None = None,
    ) -> None:
        """Handle image_artifact:created from runner and mark the artifact ready."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            raise TaskNotFoundError(task_id)

        if not self._validate_task_runner(task, runner_id):
            return

        artifact = self.image_artifacts.get_by_task_id(task_id)
        if artifact is not None:
            self.image_artifacts.mark_ready(
                artifact.id,
                runner_artifact_id=artifact_id,
                size_bytes=size_bytes,
            )
        else:
            workspace = self.workspaces.get_by_id(uuid.UUID(workspace_id))
            if workspace is None:
                raise WorkspaceNotFoundError(workspace_id)
            workspace_credentials = list(workspace.credentials.all())
            self.image_artifacts.create(
                source_workspace=workspace,
                runner_artifact_id=artifact_id,
                name=name,
                size_bytes=size_bytes,
                created_by=task.workspace.created_by if task.workspace else None,
                credentials=workspace_credentials,
            )

        if task.workspace:
            self.workspaces.update_active_operation(task.workspace, None)
        self.tasks.complete(task)
        logger.info(
            "Image artifact created: workspace=%s, artifact=%s",
            workspace_id,
            artifact_id,
        )

        self._forward_to_frontend(
            "image_artifact:created",
            {
                "workspace_id": workspace_id,
                "image_artifact_id": artifact_id,
                "name": name,
                "size_bytes": size_bytes,
            },
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def handle_image_artifact_failed(
        self,
        task_id: str,
        workspace_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Handle image_artifact:failed by marking the pending artifact as failed."""
        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        self.image_artifacts.mark_failed_by_task_id(task_id)

        if task is not None:
            if task.workspace:
                self.workspaces.update_active_operation(task.workspace, None)
            self.tasks.fail(task, error)

        logger.warning(
            "Image artifact creation failed: workspace=%s, task=%s, error=%s",
            workspace_id,
            task_id,
            error,
        )

        self._forward_to_frontend(
            "image_artifact:failed",
            {"workspace_id": workspace_id, "task_id": task_id, "error": error},
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def list_image_artifacts_for_workspace(self, workspace_id: uuid.UUID) -> list:
        """Return all artifacts captured from a workspace."""
        return list(self.image_artifacts.list_by_workspace(workspace_id))

    def list_image_artifacts_for_user(self, user) -> list:
        """Return all artifacts created by a specific user."""
        return list(self.image_artifacts.list_by_user(user))

    async def delete_image_artifact(
        self,
        image_artifact_id: uuid.UUID,
    ) -> None:
        """Delete an image artifact and dispatch cleanup to the runner if needed."""
        artifact = await sync_to_async(self.image_artifacts.get_by_id)(image_artifact_id)
        if artifact is None:
            raise ValueError(f"Image artifact '{image_artifact_id}' not found")

        if artifact.runner_image_build is not None:
            await sync_to_async(self.image_artifacts.delete)(image_artifact_id)
            logger.info("Built image artifact deleted: %s", image_artifact_id)
            return

        workspace = artifact.source_workspace
        if workspace is None:
            raise ValueError(f"Image artifact '{image_artifact_id}' has no source workspace")
        runner = workspace.runner
        if runner.is_online and artifact.runner_artifact_id:
            await self._emit_to_runner(
                runner,
                "task:delete_image_artifact",
                {
                    "task_id": str(generate_uuid()),
                    "workspace_id": str(workspace.id),
                    "image_artifact_id": artifact.runner_artifact_id,
                },
            )

        await sync_to_async(self.image_artifacts.delete)(image_artifact_id)
        logger.info("Image artifact deleted: %s", image_artifact_id)

    async def create_workspace_from_image_artifact(
        self,
        image_artifact_id: uuid.UUID,
        name: str = "",
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
        credentials: list | None = None,
        user=None,
        organization_id: uuid.UUID | None = None,
    ) -> tuple["Workspace", "Task"]:
        """Create a workspace from an image artifact.

        Credentials are automatically restored from the artifact if not
        explicitly overridden. The caller should not need to pass credentials
        when creating a workspace — they are stored on the artifact.
        """
        from apps.credentials.services import CredentialSvc

        artifact = await sync_to_async(self.image_artifacts.get_by_id)(image_artifact_id)
        if artifact is None:
            raise ValueError(f"Image artifact '{image_artifact_id}' not found")

        if artifact.status != "ready":
            raise ConflictError(f"Image artifact '{image_artifact_id}' is not ready")

        source_workspace = artifact.source_workspace
        if source_workspace is not None:
            runner = source_workspace.runner
            runtime_type = source_workspace.runtime_type
            qemu_vcpus = source_workspace.qemu_vcpus
            qemu_memory_mb = source_workspace.qemu_memory_mb
            qemu_disk_size_gb = source_workspace.qemu_disk_size_gb
        elif artifact.runner_image_build is not None:
            runner = artifact.runner_image_build.runner
            runtime_type = artifact.runner_image_build.image_definition.runtime_type
            qemu_vcpus = None
            qemu_memory_mb = None
            qemu_disk_size_gb = None
        else:
            raise ValueError(
                f"Image artifact '{image_artifact_id}' is missing its source runtime metadata"
            )

        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        if source_workspace is not None:
            self._ensure_workspace_available(source_workspace)

        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=runtime_type,
        )
        if runtime_type == RuntimeType.QEMU:
            self._validate_runner_qemu_limits(runner)
            (
                qemu_vcpus,
                qemu_memory_mb,
                qemu_disk_size_gb,
            ) = self._resolve_qemu_resources(
                runner=runner,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
            )
            await self._ensure_qemu_active_capacity(
                runner=runner,
                requested_vcpus=qemu_vcpus,
                requested_memory_mb=qemu_memory_mb,
                requested_disk_size_gb=qemu_disk_size_gb,
            )

        workspace_id = generate_uuid()
        workspace_name = self._derive_workspace_name(name, [], workspace_id)
        if not name:
            workspace_name = f"{workspace_name} (clone)"

        workspace = await sync_to_async(self.workspaces.create)(
            workspace_id=workspace_id,
            runner=runner,
            name=workspace_name,
            runtime_type=runtime_type,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            created_by=user,
        )

        # Resolve credentials: use artifact's stored credentials if none provided
        if credentials is not None:
            await sync_to_async(self.workspaces.set_credentials)(workspace, credentials)
            resolved_env_vars = env_vars or {}
            resolved_ssh_keys = ssh_keys or []
        else:
            artifact_credentials = await sync_to_async(list)(artifact.credentials.all())
            if artifact_credentials:
                await sync_to_async(self.workspaces.set_credentials)(
                    workspace, artifact_credentials
                )
                artifact_cred_ids = [c.id for c in artifact_credentials]
                credential_svc = CredentialSvc()
                resolved = await sync_to_async(credential_svc.resolve_credentials)(
                    artifact_cred_ids,
                    org_id=organization_id,
                    user=user,
                )
                resolved_env_vars = resolved.env_vars
                resolved_ssh_keys = resolved.ssh_keys
            else:
                resolved_env_vars = env_vars or {}
                resolved_ssh_keys = ssh_keys or []

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT,
            workspace=workspace,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:create_workspace_from_image_artifact",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(
                TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT
            ),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "image_artifact_id": artifact.runner_artifact_id,
                "runtime_type": runtime_type,
                "qemu_vcpus": qemu_vcpus,
                "qemu_memory_mb": qemu_memory_mb,
                "qemu_disk_size_gb": qemu_disk_size_gb,
                "env_vars": resolved_env_vars,
                "ssh_keys": resolved_ssh_keys,
            },
        )
        logger.info(
            "Dispatched create_workspace_from_image_artifact (workspace=%s, artifact=%s, task=%s)",
            workspace_id,
            image_artifact_id,
            task_id,
        )
        return workspace, task
