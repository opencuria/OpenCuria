"""
Repository layer for the runners app.

Encapsulates all database queries. Services never use the ORM directly —
they call repository methods instead. This keeps business logic decoupled
from data access and makes services easy to test with mock repositories.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db.models import Count, Exists, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from .enums import (
    AgentCommandPhase,
    RunnerStatus,
    SessionStatus,
    TaskStatus,
    TaskType,
    WorkspaceOperation,
    WorkspaceStatus,
)
from .models import (
    AgentCommand,
    AgentDefinition,
    Chat,
    ImageArtifact,
    ImageDefinition,
    Runner,
    RunnerImageBuild,
    RunnerSystemMetrics,
    Session,
    Task,
    Workspace,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_is_read(last_session: dict | None) -> bool:
    """
    Return True when a conversation has nothing new for the user to read.

    Rules:
    - No session at all → read (nothing to show).
    - Session is pending/running → read (not finished yet, kanban shows in
      "Am Arbeiten"; read-tracking doesn't apply).
    - Session completed/failed with no ``read_at`` → unread.
    - Session completed/failed with ``read_at`` → read.
    """
    if last_session is None:
        return True
    if last_session["status"] not in (SessionStatus.COMPLETED, SessionStatus.FAILED):
        return True
    return last_session["read_at"] is not None


def _touch_conversation_activity(
    *,
    workspace_id: uuid.UUID | None = None,
    chat_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> None:
    """Update workspace/chat ``updated_at`` so "last activity" stays accurate.
    
    Either workspace_id or chat_id must be provided.
    If only chat_id is given, workspace_id is derived from the chat.
    """
    activity_at = at or timezone.now()
    if chat_id is not None:
        Chat.objects.filter(id=chat_id).update(updated_at=activity_at)
        # Derive workspace_id from chat if not provided
        if workspace_id is None:
            chat = Chat.objects.filter(id=chat_id).first()
            if chat:
                workspace_id = chat.workspace_id
    if workspace_id is not None:
        Workspace.objects.filter(id=workspace_id).update(
            last_activity_at=activity_at,
            updated_at=activity_at,
        )


# ---------------------------------------------------------------------------
# Runner Repository
# ---------------------------------------------------------------------------


class RunnerRepository:
    """Data access for Runner records."""

    @staticmethod
    def get_by_id(runner_id: uuid.UUID) -> Runner | None:
        """Fetch a runner by its ID, or None if not found."""
        return Runner.objects.filter(id=runner_id).first()

    @staticmethod
    def get_by_token_hash(token_hash: str) -> Runner | None:
        """Fetch a runner by its hashed API token."""
        return Runner.objects.filter(api_token_hash=token_hash).first()

    @staticmethod
    def list_all() -> QuerySet[Runner]:
        """Return all runners ordered by creation date."""
        return Runner.objects.all()

    @staticmethod
    def list_online() -> QuerySet[Runner]:
        """Return all online runners."""
        return Runner.objects.filter(status=RunnerStatus.ONLINE)

    @staticmethod
    def create(
        *,
        name: str = "",
        api_token_hash: str,
        organization=None,
    ) -> Runner:
        """Create a new runner record."""
        return Runner.objects.create(
            name=name,
            api_token_hash=api_token_hash,
            organization=organization,
        )

    @staticmethod
    def set_online(
        runner: Runner,
        *,
        sid: str,
        available_runtimes: list[str] | None = None,
    ) -> Runner:
        """Mark a runner as online with its Socket.IO session ID."""
        runner.status = RunnerStatus.ONLINE
        runner.sid = sid
        if available_runtimes is not None:
            runner.available_runtimes = available_runtimes
        runner.connected_at = timezone.now()
        runner.disconnected_at = None
        runner.save(
            update_fields=[
                "status", "sid", "available_runtimes",
                "connected_at", "disconnected_at", "updated_at",
            ]
        )
        return runner

    @staticmethod
    def set_offline(runner: Runner) -> Runner:
        """Mark a runner as offline."""
        runner.status = RunnerStatus.OFFLINE
        runner.sid = ""
        runner.disconnected_at = timezone.now()
        runner.save(
            update_fields=["status", "sid", "disconnected_at", "updated_at"]
        )
        return runner

    @staticmethod
    def list_by_organization(organization_id: uuid.UUID) -> QuerySet[Runner]:
        """Return all runners for a specific organization."""
        return Runner.objects.filter(organization_id=organization_id)

    @staticmethod
    def update_heartbeat(runner: Runner) -> Runner:
        """Update the last heartbeat timestamp for a runner."""
        runner.last_heartbeat_at = timezone.now()
        runner.save(update_fields=["last_heartbeat_at", "updated_at"])
        return runner

    @staticmethod
    def update_qemu_settings(runner: Runner, **fields) -> Runner:
        """Update QEMU resource settings on a runner."""
        for key, value in fields.items():
            setattr(runner, key, value)
        runner.save(update_fields=[*fields.keys(), "updated_at"])
        return runner


class RunnerSystemMetricsRepository:
    """Data access for RunnerSystemMetrics records."""

    @staticmethod
    def create(
        *,
        runner: Runner,
        timestamp,
        cpu_usage_percent: float,
        ram_used_bytes: int,
        ram_total_bytes: int,
        disk_used_bytes: int,
        disk_total_bytes: int,
        vm_metrics: dict[str, Any] | None = None,
    ) -> RunnerSystemMetrics:
        """Persist a new system metrics snapshot."""
        return RunnerSystemMetrics.objects.create(
            runner=runner,
            timestamp=timestamp,
            cpu_usage_percent=cpu_usage_percent,
            ram_used_bytes=ram_used_bytes,
            ram_total_bytes=ram_total_bytes,
            disk_used_bytes=disk_used_bytes,
            disk_total_bytes=disk_total_bytes,
            vm_metrics=vm_metrics,
        )

    @staticmethod
    def get_latest(runner_id: uuid.UUID) -> RunnerSystemMetrics | None:
        """Return the most recent metrics snapshot for the given runner."""
        return (
            RunnerSystemMetrics.objects.filter(runner_id=runner_id)
            .order_by("-timestamp")
            .first()
        )

    @staticmethod
    def get_history(runner_id: uuid.UUID, since: datetime) -> QuerySet[RunnerSystemMetrics]:
        """Return all metrics since a given timestamp."""
        return (
            RunnerSystemMetrics.objects.filter(
                runner_id=runner_id,
                timestamp__gte=since,
            )
            .order_by("timestamp")
        )

    @staticmethod
    def purge_old(runner_id: uuid.UUID, keep_hours: int = 24) -> int:
        """Delete metrics older than *keep_hours* hours. Returns count deleted."""
        from django.utils import timezone as tz
        from datetime import timedelta

        cutoff = tz.now() - timedelta(hours=keep_hours)
        deleted, _ = RunnerSystemMetrics.objects.filter(
            runner_id=runner_id, timestamp__lt=cutoff
        ).delete()
        return deleted


# ---------------------------------------------------------------------------
# Workspace Repository
# ---------------------------------------------------------------------------

_ACTIVE_SESSION_EXISTS = Exists(
    Session.objects.filter(
        chat__workspace=OuterRef("pk"),
        status__in=[SessionStatus.PENDING, SessionStatus.RUNNING],
    )
)


class WorkspaceRepository:
    """Data access for Workspace records."""

    @staticmethod
    def get_by_id(workspace_id: uuid.UUID) -> Workspace | None:
        """Fetch a workspace by its ID."""
        return (
            Workspace.objects.filter(id=workspace_id)
            .select_related("runner", "runner__organization", "created_by")
            .prefetch_related("credentials__service")
            .annotate(has_active_session=_ACTIVE_SESSION_EXISTS)
            .first()
        )

    @staticmethod
    def list_all() -> QuerySet[Workspace]:
        """Return all workspaces."""
        return (
            Workspace.objects.select_related("runner", "runner__organization")
            .prefetch_related("credentials__service")
            .annotate(has_active_session=_ACTIVE_SESSION_EXISTS)
        )

    @staticmethod
    def list_by_runner(runner_id: uuid.UUID) -> QuerySet[Workspace]:
        """Return all workspaces for a specific runner."""
        return (
            Workspace.objects.filter(runner_id=runner_id)
            .select_related("runner", "runner__organization")
            .prefetch_related("credentials__service")
            .annotate(has_active_session=_ACTIVE_SESSION_EXISTS)
        )

    @staticmethod
    def create(
        *,
        workspace_id: uuid.UUID,
        runner: Runner,
        name: str,
        runtime_type: str = "docker",
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        base_image_artifact=None,
        created_by=None,
    ) -> Workspace:
        """Create a new workspace record."""
        workspace = Workspace.objects.create(
            id=workspace_id,
            runner=runner,
            name=name,
            runtime_type=runtime_type,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            base_image_artifact=base_image_artifact,
            status=WorkspaceStatus.CREATING,
            active_operation=WorkspaceOperation.CREATING,
            created_by=created_by,
        )
        workspace.last_activity_at = workspace.created_at
        workspace.save(update_fields=["last_activity_at"])
        return workspace

    @staticmethod
    def set_credentials(workspace: Workspace, credentials: list) -> Workspace:
        """Replace the credentials attached to a workspace."""
        workspace.credentials.set(credentials)
        workspace.save(update_fields=["updated_at"])
        return workspace

    @staticmethod
    def touch_activity(
        workspace: Workspace,
        *,
        at=None,
    ) -> Workspace:
        """Update the workspace activity timestamp without changing its status."""
        activity_at = at or timezone.now()
        workspace.last_activity_at = activity_at
        workspace.updated_at = activity_at
        workspace.save(update_fields=["last_activity_at", "updated_at"])
        return workspace

    @staticmethod
    def list_by_organization(organization_id: uuid.UUID) -> QuerySet[Workspace]:
        """Return all workspaces for runners in a specific organization."""
        return (
            Workspace.objects.filter(runner__organization_id=organization_id)
            .select_related("runner", "runner__organization")
            .prefetch_related("credentials__service")
            .annotate(has_active_session=_ACTIVE_SESSION_EXISTS)
        )

    @staticmethod
    def list_by_user(user_id: int) -> QuerySet[Workspace]:
        """Return all workspaces created by a specific user."""
        return (
            Workspace.objects.filter(created_by_id=user_id)
            .select_related("runner", "runner__organization")
            .prefetch_related("credentials__service")
            .annotate(has_active_session=_ACTIVE_SESSION_EXISTS)
        )

    @staticmethod
    def update_status(
        workspace: Workspace,
        status: WorkspaceStatus,
    ) -> Workspace:
        """Update a workspace's status."""
        workspace.status = status
        workspace.save(update_fields=["status", "updated_at"])
        return workspace

    @staticmethod
    def update_active_operation(
        workspace: Workspace,
        active_operation: WorkspaceOperation | None,
    ) -> Workspace:
        """Update the currently active blocking operation for a workspace."""
        workspace.active_operation = active_operation
        workspace.save(update_fields=["active_operation", "updated_at"])
        return workspace

    @staticmethod
    def update_name(workspace: Workspace, name: str) -> Workspace:
        """Update a workspace's name."""
        workspace.name = name
        workspace.save(update_fields=["name", "updated_at"])
        return workspace

    @staticmethod
    def update_qemu_resources(
        workspace: Workspace,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
    ) -> Workspace:
        """Persist QEMU workspace resource settings."""
        workspace.qemu_vcpus = qemu_vcpus
        workspace.qemu_memory_mb = qemu_memory_mb
        workspace.qemu_disk_size_gb = qemu_disk_size_gb
        workspace.save(update_fields=["qemu_vcpus", "qemu_memory_mb", "qemu_disk_size_gb", "updated_at"])
        return workspace

    @staticmethod
    def list_running_qemu_by_runner(runner_id: uuid.UUID) -> QuerySet[Workspace]:
        """Return active QEMU workspaces for a runner."""
        return Workspace.objects.filter(
            runner_id=runner_id,
            runtime_type="qemu",
            status=WorkspaceStatus.RUNNING,
        )


# ---------------------------------------------------------------------------
# Session Repository
# ---------------------------------------------------------------------------


class SessionRepository:
    """Data access for Session records."""

    @staticmethod
    def get_by_id(session_id: uuid.UUID) -> Session | None:
        """Fetch a session by its ID."""
        return Session.objects.filter(id=session_id).select_related("chat__workspace").first()

    @staticmethod
    def list_by_workspace(workspace_id: uuid.UUID) -> QuerySet[Session]:
        """Return all sessions for a specific workspace."""
        return Session.objects.filter(chat__workspace_id=workspace_id).prefetch_related(
            "session_skills"
        )

    @staticmethod
    def create(
        *,
        session_id: uuid.UUID,
        chat: Chat,
        prompt: str,
        agent_model: str = "",
        agent_options: dict | None = None,
    ) -> Session:
        """Create a new session record in RUNNING state."""
        session = Session.objects.create(
            id=session_id,
            chat=chat,
            prompt=prompt,
            agent_model=agent_model,
            agent_options=agent_options or {},
            status=SessionStatus.RUNNING,
        )
        _touch_conversation_activity(
            chat_id=session.chat_id,
            at=session.created_at,
        )
        return session

    @staticmethod
    def list_by_chat(chat_id: uuid.UUID) -> QuerySet[Session]:
        """Return all sessions for a specific chat."""
        return Session.objects.filter(chat_id=chat_id).prefetch_related(
            "session_skills"
        )

    @staticmethod
    def get_latest_for_workspace(workspace_id: uuid.UUID) -> Session | None:
        """Return the newest session for a workspace, if any."""
        return Session.objects.filter(chat__workspace_id=workspace_id).first()

    @staticmethod
    def has_active_for_workspace(workspace_id: uuid.UUID) -> bool:
        """Return True if the workspace has a pending or running session."""
        return Session.objects.filter(
            chat__workspace_id=workspace_id,
            status__in=[SessionStatus.PENDING, SessionStatus.RUNNING],
        ).exists()

    @staticmethod
    def has_any_for_chat(chat_id: uuid.UUID) -> bool:
        """Return True if the chat already has at least one session."""
        return Session.objects.filter(chat_id=chat_id).exists()

    @staticmethod
    def has_any_successful_for_chat(chat_id: uuid.UUID) -> bool:
        """Return True if the chat has at least one successful run_prompt session.

        A successful session is a completed run_prompt session.
        """
        return Session.objects.filter(
            chat_id=chat_id,
            status=SessionStatus.COMPLETED,
        ).exists()

    @staticmethod
    def append_output(session: Session, line: str) -> None:
        """Append a line of output to a session."""
        if session.output:
            session.output += "\n" + line
        else:
            session.output = line
        session.save(update_fields=["output"])
        _touch_conversation_activity(
            chat_id=session.chat_id,
        )

    @staticmethod
    def complete(session: Session, output: str | None = None) -> Session:
        """Mark a session as completed."""
        session.status = SessionStatus.COMPLETED
        session.completed_at = timezone.now()
        session.error_message = None
        session.read_at = None
        if output is not None:
            session.output = output
        session.save(
            update_fields=["status", "completed_at", "output", "error_message", "read_at"]
        )
        _touch_conversation_activity(
            chat_id=session.chat_id,
            at=session.completed_at,
        )
        return session

    @staticmethod
    def fail(
        session: Session,
        output: str | None = None,
        error_message: str | None = None,
    ) -> Session:
        """Mark a session as failed."""
        session.status = SessionStatus.FAILED
        session.completed_at = timezone.now()
        session.error_message = error_message
        session.read_at = None
        if output is not None:
            session.output = output
        session.save(
            update_fields=["status", "completed_at", "output", "error_message", "read_at"]
        )
        _touch_conversation_activity(
            chat_id=session.chat_id,
            at=session.completed_at,
        )
        return session

    @staticmethod
    def mark_read(session: Session) -> Session:
        """Mark a completed/failed session as read (user opened the chat)."""
        if session.status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
            session.read_at = timezone.now()
            session.save(update_fields=["read_at"])
            _touch_conversation_activity(
                chat_id=session.chat_id,
            )
        return session

    @staticmethod
    def mark_unread(session: Session) -> Session:
        """Mark a completed/failed session as unread again."""
        if session.status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
            session.read_at = None
            session.save(update_fields=["read_at"])
            _touch_conversation_activity(
                chat_id=session.chat_id,
            )
        return session


# ---------------------------------------------------------------------------
# Task Repository
# ---------------------------------------------------------------------------


class TaskRepository:
    """Data access for Task records."""

    @staticmethod
    def get_by_id(task_id: uuid.UUID) -> Task | None:
        """Fetch a task by its ID."""
        return (
            Task.objects.filter(id=task_id)
            .select_related("runner", "workspace", "session")
            .first()
        )

    @staticmethod
    def get_active_run_task_for_session(session_id: uuid.UUID) -> Task | None:
        """Return the active run_prompt task for a session, if any."""
        return (
            Task.objects.filter(
                session_id=session_id,
                type=TaskType.RUN_PROMPT,
                status__in=[TaskStatus.PENDING, TaskStatus.IN_PROGRESS],
            )
            .select_related("runner", "workspace", "session")
            .order_by("-created_at")
            .first()
        )

    @staticmethod
    def create(
        *,
        task_id: uuid.UUID,
        runner: Runner,
        task_type: TaskType,
        workspace: Workspace | None = None,
        session: Session | None = None,
    ) -> Task:
        """Create a new task record."""
        return Task.objects.create(
            id=task_id,
            runner=runner,
            workspace=workspace,
            session=session,
            type=task_type,
            status=TaskStatus.PENDING,
        )

    @staticmethod
    def mark_in_progress(task: Task) -> Task:
        """Mark a task as in progress."""
        task.status = TaskStatus.IN_PROGRESS
        task.save(update_fields=["status"])
        return task

    @staticmethod
    def complete(task: Task) -> Task:
        """Mark a task as completed."""
        task.status = TaskStatus.COMPLETED
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "completed_at"])
        return task

    @staticmethod
    def fail(task: Task, error: str) -> Task:
        """Mark a task as failed with an error message."""
        task.status = TaskStatus.FAILED
        task.error = error
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "error", "completed_at"])
        return task


# ---------------------------------------------------------------------------
# Chat Repository
# ---------------------------------------------------------------------------


class ChatRepository:
    """Data access for Chat records."""

    @staticmethod
    def get_by_id(chat_id: uuid.UUID) -> Chat | None:
        """Fetch a chat by its ID."""
        return Chat.objects.filter(id=chat_id).select_related("workspace", "agent_definition").first()

    @staticmethod
    def list_by_workspace(workspace_id: uuid.UUID) -> QuerySet[Chat]:
        """Return all chats for a workspace."""
        return Chat.objects.filter(workspace_id=workspace_id).select_related("agent_definition")

    @staticmethod
    def get_latest_for_workspace_agent(
        workspace_id: uuid.UUID,
        agent_definition_id: uuid.UUID,
    ) -> Chat | None:
        """Return the newest chat for a workspace+agent pair."""
        return (
            Chat.objects.filter(
                workspace_id=workspace_id,
                agent_definition_id=agent_definition_id,
            )
            .order_by("-created_at", "-id")
            .first()
        )

    @staticmethod
    def create(
        *,
        chat_id: uuid.UUID,
        workspace: Workspace,
        name: str = "",
        agent_definition: AgentDefinition | None = None,
        agent_type: str = "",
    ) -> Chat:
        """Create a new chat record."""
        chat = Chat.objects.create(
            id=chat_id,
            workspace=workspace,
            name=name,
            agent_definition=agent_definition,
            agent_type=agent_type,
        )
        _touch_conversation_activity(
            workspace_id=workspace.id,
            chat_id=chat.id,
            at=chat.created_at,
        )
        return chat

    @staticmethod
    def update_name(chat: Chat, name: str) -> Chat:
        """Update a chat's name."""
        chat.name = name
        chat.save(update_fields=["name", "updated_at"])
        _touch_conversation_activity(
            workspace_id=chat.workspace_id,
            chat_id=chat.id,
        )
        return chat

    @staticmethod
    def delete(chat_id: uuid.UUID) -> None:
        """Delete a chat and its sessions (cascade)."""
        Chat.objects.filter(id=chat_id).delete()


# ---------------------------------------------------------------------------
# Agent Repository
# ---------------------------------------------------------------------------


class AgentRepository:
    """Data access for AgentDefinition and AgentCommand records."""

    @staticmethod
    def get_by_id(agent_id: uuid.UUID) -> AgentDefinition | None:
        """Fetch an agent definition by ID, with prefetched commands."""
        return (
            AgentDefinition.objects.filter(id=agent_id)
            .prefetch_related("commands")
            .first()
        )

    @staticmethod
    def get_visible_by_id(
        agent_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
    ) -> AgentDefinition | None:
        """Fetch an org-visible agent definition by ID, honoring activation."""
        from .models import OrgAgentDefinitionActivation

        qs = AgentDefinition.objects.filter(id=agent_id).prefetch_related(
            "commands", "required_credential_services"
        )
        if organization_id is None:
            return qs.first()

        activated_ids = OrgAgentDefinitionActivation.objects.filter(
            organization_id=organization_id,
            agent_definition_id=agent_id,
        ).values_list("agent_definition_id", flat=True)
        return qs.filter(
            Q(organization__isnull=True) | Q(organization_id=organization_id),
            id__in=activated_ids,
        ).first()

    @staticmethod
    def list_all() -> QuerySet[AgentDefinition]:
        """Return all agent definitions."""
        return AgentDefinition.objects.prefetch_related("commands").all()

    @staticmethod
    def get_configure_commands(agent_id: uuid.UUID) -> list[AgentCommand]:
        """Return configure-phase commands for an agent, ordered by execution order."""
        return list(
            AgentCommand.objects.filter(
                agent_id=agent_id,
                phase=AgentCommandPhase.CONFIGURE,
            ).order_by("order")
        )

    @staticmethod
    def get_run_command(agent_id: uuid.UUID) -> AgentCommand | None:
        """Return the single run-phase command template for an agent."""
        return (
            AgentCommand.objects.filter(
                agent_id=agent_id,
                phase=AgentCommandPhase.RUN,
            ).first()
        )

    @staticmethod
    def get_run_first_command(agent_id: uuid.UUID) -> AgentCommand | None:
        """Return the run_first-phase command template for an agent, if defined.

        This command is used for the very first message in a new chat. Agents
        that need special handling for session initialisation (e.g. Claude Code's
        ``--session-id`` flag) can define this phase. Falls back to the regular
        ``run`` command when absent.
        """
        return (
            AgentCommand.objects.filter(
                agent_id=agent_id,
                phase=AgentCommandPhase.RUN_FIRST,
            ).first()
        )

    @staticmethod
    def list_agent_names() -> list[str]:
        """Return all agent definition names as a sorted list."""
        return sorted(
            AgentDefinition.objects.values_list("name", flat=True)
        )

    @staticmethod
    def list_all_with_credential_slugs(
        organization_id: uuid.UUID | None = None,
    ) -> list:
        """Return agent definitions visible to an org, filtered by activation.

        When ``organization_id`` is provided, only definitions that are
        activated for that organization are returned (both standard and
        org-specific). Without an org_id, all definitions are returned.
        """
        from .models import OrgAgentDefinitionActivation

        qs = AgentDefinition.objects.prefetch_related(
            "required_credential_services", "commands"
        )

        if organization_id is not None:
            activated_ids = list(
                OrgAgentDefinitionActivation.objects.filter(
                    organization_id=organization_id
                ).values_list("agent_definition_id", flat=True)
            )
            qs = qs.filter(
                Q(organization__isnull=True) | Q(organization_id=organization_id),
                id__in=activated_ids,
            )

        return list(qs)


# ---------------------------------------------------------------------------
# Conversation Repository
# ---------------------------------------------------------------------------


class ConversationRepository:
    """Data access for the conversation list (chats + workspace fallbacks)."""

    @staticmethod
    def list_for_user(
        organization_id: uuid.UUID,
        user_id: int,
        is_admin: bool,
    ) -> list[dict]:
        """
        Return all conversations for a user, sorted by last activity DESC.

        Each Chat in the org becomes one row. Workspaces without any Chat
        (single-chat / empty workspaces) are included as fallback rows.
        Uses Subquery annotations to avoid N+1 queries.
        Each row includes ``is_read``: True when the last finished session has
        already been opened by the user (or there is nothing finished yet).
        """
        # Subquery: latest session per chat
        latest_chat_session = Session.objects.filter(
            chat_id=OuterRef("pk"),
        ).order_by("-created_at")

        chat_qs = Chat.objects.select_related("workspace", "workspace__runner").filter(
            workspace__runner__organization_id=organization_id
        )
        if not is_admin:
            chat_qs = chat_qs.filter(workspace__created_by_id=user_id)

        chat_qs = chat_qs.annotate(
            _session_count=Count("sessions"),
            _last_session_id=Subquery(latest_chat_session.values("id")[:1]),
            _last_session_prompt=Subquery(latest_chat_session.values("prompt")[:1]),
            _last_session_status=Subquery(latest_chat_session.values("status")[:1]),
            _last_session_read_at=Subquery(latest_chat_session.values("read_at")[:1]),
            _last_session_created_at=Subquery(
                latest_chat_session.values("created_at")[:1]
            ),
            _last_session_completed_at=Subquery(
                latest_chat_session.values("completed_at")[:1]
            ),
        )

        rows: list[dict] = []

        for chat in chat_qs:
            ws = chat.workspace
            last_session = None
            if chat._last_session_id:
                last_session = {
                    "id": chat._last_session_id,
                    "prompt": chat._last_session_prompt,
                    "status": chat._last_session_status,
                    "read_at": chat._last_session_read_at,
                    "created_at": chat._last_session_created_at,
                }
            last_activity_at = (
                chat._last_session_completed_at
                or chat._last_session_created_at
                or chat.updated_at
            )
            rows.append(
                {
                    "chat_id": chat.id,
                    "workspace_id": ws.id,
                    "workspace_name": ws.name,
                    "workspace_status": ws.status,
                    "agent_definition_id": chat.agent_definition_id,
                    "agent_type": chat.agent_type,
                    "chat_name": chat.name,
                    "last_session": last_session,
                    "session_count": chat._session_count,
                    "updated_at": last_activity_at,
                    "is_read": _compute_is_read(last_session),
                }
            )

        # Fallback: workspaces that have no chats at all
        has_any_chat = Exists(Chat.objects.filter(workspace_id=OuterRef("pk")))

        # Subquery: latest session per workspace (no chat FK) - legacy, should be zero rows
        latest_ws_session = Session.objects.filter(
            chat__workspace_id=OuterRef("pk"),
            chat__isnull=True,
        ).order_by("-created_at")

        ws_qs = (
            Workspace.objects.select_related("runner")
            .filter(runner__organization_id=organization_id)
            .annotate(_has_chats=has_any_chat)
            .filter(_has_chats=False)
        )
        if not is_admin:
            ws_qs = ws_qs.filter(created_by_id=user_id)

        ws_qs = ws_qs.annotate(
            _session_count=Count("chats__sessions"),
            _last_session_id=Subquery(latest_ws_session.values("id")[:1]),
            _last_session_prompt=Subquery(latest_ws_session.values("prompt")[:1]),
            _last_session_status=Subquery(latest_ws_session.values("status")[:1]),
            _last_session_read_at=Subquery(latest_ws_session.values("read_at")[:1]),
            _last_session_created_at=Subquery(
                latest_ws_session.values("created_at")[:1]
            ),
            _last_session_completed_at=Subquery(
                latest_ws_session.values("completed_at")[:1]
            ),
        )

        for ws in ws_qs:
            last_session = None
            if ws._last_session_id:
                last_session = {
                    "id": ws._last_session_id,
                    "prompt": ws._last_session_prompt,
                    "status": ws._last_session_status,
                    "read_at": ws._last_session_read_at,
                    "created_at": ws._last_session_created_at,
                }
            last_activity_at = (
                ws._last_session_completed_at
                or ws._last_session_created_at
                or ws.updated_at
            )
            rows.append(
                {
                    "chat_id": None,
                    "workspace_id": ws.id,
                    "workspace_name": ws.name,
                    "workspace_status": ws.status,
                    "agent_definition_id": None,
                    "agent_type": "",
                    "chat_name": "",
                    "last_session": last_session,
                    "session_count": ws._session_count,
                    "updated_at": last_activity_at,
                    "is_read": _compute_is_read(last_session),
                }
            )

        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return rows


class ImageArtifactRepository:
    """Data access for ImageArtifact records."""

    @staticmethod
    def create(
        *,
        source_workspace: "Workspace | None",
        runner_artifact_id: str,
        name: str,
        size_bytes: int = 0,
        artifact_kind: str = ImageArtifact.ArtifactKind.CAPTURED,
        runner_image_build: RunnerImageBuild | None = None,
        created_by=None,
        credentials: list | None = None,
    ) -> "ImageArtifact":
        """Create a new artifact record (immediately ready)."""
        artifact = ImageArtifact.objects.create(
            source_workspace=source_workspace,
            runner_artifact_id=runner_artifact_id,
            name=name,
            size_bytes=size_bytes,
            artifact_kind=artifact_kind,
            runner_image_build=runner_image_build,
            created_by=created_by,
            status=ImageArtifact.ArtifactStatus.READY,
        )
        if credentials:
            artifact.credentials.set(credentials)
        return artifact

    @staticmethod
    def create_pending(
        *,
        source_workspace: "Workspace | None",
        name: str,
        creating_task_id: str,
        artifact_kind: str = ImageArtifact.ArtifactKind.CAPTURED,
        runner_image_build: RunnerImageBuild | None = None,
        created_by=None,
        credentials: list | None = None,
    ) -> "ImageArtifact":
        """Create an artifact record in 'creating' state before the runner finishes."""
        artifact = ImageArtifact.objects.create(
            source_workspace=source_workspace,
            runner_artifact_id="",
            name=name,
            size_bytes=0,
            artifact_kind=artifact_kind,
            runner_image_build=runner_image_build,
            created_by=created_by,
            status=ImageArtifact.ArtifactStatus.CREATING,
            creating_task_id=creating_task_id,
        )
        if credentials:
            artifact.credentials.set(credentials)
        return artifact

    @staticmethod
    def get_by_task_id(task_id: str) -> "ImageArtifact | None":
        """Find the artifact record being created by a given task."""
        return (
            ImageArtifact.objects.filter(creating_task_id=task_id)
            .select_related(
                "source_workspace",
                "source_workspace__runner",
                "created_by",
                "runner_image_build",
                "runner_image_build__runner",
                "runner_image_build__image_definition",
            )
            .prefetch_related("credentials__service")
            .first()
        )

    @staticmethod
    def mark_ready(
        artifact_id, *, runner_artifact_id: str, size_bytes: int
    ) -> None:
        """Update a creating artifact to ready once the runner reports success."""
        ImageArtifact.objects.filter(id=artifact_id).update(
            status=ImageArtifact.ArtifactStatus.READY,
            runner_artifact_id=runner_artifact_id,
            size_bytes=size_bytes,
        )

    @staticmethod
    def mark_failed(artifact_id) -> None:
        """Mark an artifact as failed."""
        ImageArtifact.objects.filter(id=artifact_id).update(
            status=ImageArtifact.ArtifactStatus.FAILED,
        )

    @staticmethod
    def mark_failed_by_task_id(task_id: str) -> None:
        """Mark any creating artifact associated with task_id as failed."""
        ImageArtifact.objects.filter(
            creating_task_id=task_id,
            status=ImageArtifact.ArtifactStatus.CREATING,
        ).update(status=ImageArtifact.ArtifactStatus.FAILED)

    @staticmethod
    def timeout_stale(*, timeout_hours: int = 1) -> int:
        """Mark stale 'creating' artifacts (older than timeout_hours) as failed.

        Returns the number of artifacts that were timed out.
        """
        from datetime import timedelta

        cutoff = timezone.now() - timedelta(hours=timeout_hours)
        count = ImageArtifact.objects.filter(
            status=ImageArtifact.ArtifactStatus.CREATING,
            created_at__lt=cutoff,
        ).update(status=ImageArtifact.ArtifactStatus.FAILED)
        return count

    @staticmethod
    def update_name(artifact_id: uuid.UUID, name: str) -> bool:
        """Rename an artifact. Returns True if updated."""
        count = ImageArtifact.objects.filter(id=artifact_id).update(name=name)
        return count > 0

    @staticmethod
    def get_by_id(artifact_id: uuid.UUID) -> "ImageArtifact | None":
        """Fetch an artifact by ID, including source and runner info."""
        return (
            ImageArtifact.objects.filter(id=artifact_id)
            .select_related(
                "source_workspace",
                "source_workspace__runner",
                "created_by",
                "runner_image_build",
                "runner_image_build__runner",
                "runner_image_build__image_definition",
            )
            .prefetch_related("credentials__service")
            .first()
        )

    @staticmethod
    def get_by_runner_image_build_id(
        runner_image_build_id: uuid.UUID,
    ) -> "ImageArtifact | None":
        """Fetch a built artifact by its runner build relation."""
        return (
            ImageArtifact.objects.filter(runner_image_build_id=runner_image_build_id)
            .select_related(
                "source_workspace",
                "source_workspace__runner",
                "created_by",
                "runner_image_build",
                "runner_image_build__runner",
                "runner_image_build__image_definition",
            )
            .prefetch_related("credentials__service")
            .first()
        )

    @staticmethod
    def list_by_workspace(workspace_id: uuid.UUID) -> "QuerySet[ImageArtifact]":
        """Return all artifacts captured from a workspace."""
        return ImageArtifact.objects.filter(source_workspace_id=workspace_id).select_related(
            "source_workspace",
            "source_workspace__runner",
            "created_by",
            "runner_image_build",
            "runner_image_build__runner",
            "runner_image_build__image_definition",
        ).prefetch_related("credentials__service")

    @staticmethod
    def list_by_user(user) -> "QuerySet[ImageArtifact]":
        """Return all artifacts created by a specific user."""
        return ImageArtifact.objects.filter(created_by=user).select_related(
            "source_workspace",
            "source_workspace__runner",
            "created_by",
            "runner_image_build",
            "runner_image_build__runner",
            "runner_image_build__image_definition",
        ).prefetch_related("credentials__service")

    @staticmethod
    def delete(artifact_id: uuid.UUID) -> bool:
        """Delete an artifact record. Returns True if deleted.

        Clears the base_image_artifact FK on non-active workspaces first to
        avoid ProtectedError from the PROTECT constraint.  The service layer
        already blocks deletion when *active* workspaces still reference the
        artifact, so only inactive (removed / error) references remain here.
        """
        inactive_statuses = [
            WorkspaceStatus.REMOVED,
            WorkspaceStatus.FAILED,
        ]
        Workspace.objects.filter(
            base_image_artifact_id=artifact_id,
            status__in=inactive_statuses,
        ).update(base_image_artifact=None)

        count, _ = ImageArtifact.objects.filter(id=artifact_id).delete()
        return count > 0

    @staticmethod
    def mark_retired(artifact_id: uuid.UUID) -> bool:
        """Mark an artifact as retired (no new workspaces, existing ones unaffected)."""
        count = ImageArtifact.objects.filter(
            id=artifact_id,
            status__in=[
                ImageArtifact.ArtifactStatus.READY,
            ],
        ).update(status=ImageArtifact.ArtifactStatus.RETIRED)
        return count > 0

    @staticmethod
    def unretire(artifact_id: uuid.UUID) -> bool:
        """Restore a retired artifact to ready status."""
        count = ImageArtifact.objects.filter(
            id=artifact_id,
            status=ImageArtifact.ArtifactStatus.RETIRED,
        ).update(status=ImageArtifact.ArtifactStatus.READY)
        return count > 0

    @staticmethod
    def mark_pending_delete(artifact_id: uuid.UUID) -> bool:
        """Mark an artifact as pending deletion (awaiting runner cleanup)."""
        count = ImageArtifact.objects.filter(
            id=artifact_id,
            status__in=[
                ImageArtifact.ArtifactStatus.READY,
                ImageArtifact.ArtifactStatus.RETIRED,
                ImageArtifact.ArtifactStatus.FAILED,
            ],
        ).update(status=ImageArtifact.ArtifactStatus.PENDING_DELETE)
        return count > 0

    @staticmethod
    def mark_deleted(artifact_id: uuid.UUID) -> bool:
        """Mark an artifact as fully deleted after runner cleanup confirmation."""
        from django.utils import timezone as tz

        count = ImageArtifact.objects.filter(
            id=artifact_id,
            status=ImageArtifact.ArtifactStatus.PENDING_DELETE,
        ).update(
            status=ImageArtifact.ArtifactStatus.DELETED,
            deleted_at=tz.now(),
        )
        return count > 0

    @staticmethod
    def has_dependent_workspaces(artifact_id: uuid.UUID) -> bool:
        """Check if any active workspace depends on this artifact."""
        active_statuses = [
            WorkspaceStatus.CREATING,
            WorkspaceStatus.RUNNING,
            WorkspaceStatus.STOPPED,
        ]
        return Workspace.objects.filter(
            base_image_artifact_id=artifact_id,
            status__in=active_statuses,
        ).exists()

    @staticmethod
    def count_dependent_workspaces(artifact_id: uuid.UUID) -> int:
        """Count active workspaces depending on this artifact."""
        active_statuses = [
            WorkspaceStatus.CREATING,
            WorkspaceStatus.RUNNING,
            WorkspaceStatus.STOPPED,
        ]
        return Workspace.objects.filter(
            base_image_artifact_id=artifact_id,
            status__in=active_statuses,
        ).count()


class ImageDefinitionRepository:
    """Data access for image definition records."""

    @staticmethod
    def list_by_org(organization_id: uuid.UUID) -> QuerySet[ImageDefinition]:
        return ImageDefinition.objects.filter(
            Q(organization__isnull=True) | Q(organization_id=organization_id)
        ).order_by(
            "name", "-updated_at", "-created_at"
        )

    @staticmethod
    def get_by_id(image_definition_id: uuid.UUID) -> ImageDefinition | None:
        return ImageDefinition.objects.filter(id=image_definition_id).first()

    @staticmethod
    def get_by_id_and_org(
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> ImageDefinition | None:
        """Fetch a visible image definition scoped to an organization."""
        return ImageDefinition.objects.filter(
            id=image_definition_id,
        ).filter(
            Q(organization__isnull=True) | Q(organization_id=organization_id)
        ).first()


class RunnerImageBuildRepository:
    """Data access for runner image build records."""

    @staticmethod
    def list_for_definition(
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
    ) -> QuerySet[RunnerImageBuild]:
        """List runner image builds, optionally scoped to an organization."""
        queryset = RunnerImageBuild.objects.filter(
            image_definition_id=image_definition_id
        )
        if organization_id is not None:
            queryset = queryset.filter(
                Q(image_definition__organization_id=organization_id)
                | Q(image_definition__organization__isnull=True)
            )
        return queryset.select_related("runner", "image_definition", "build_task")

    @staticmethod
    def get(
        image_definition_id: uuid.UUID,
        runner_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
    ) -> RunnerImageBuild | None:
        """Fetch one runner image build, optionally scoped to an organization."""
        queryset = RunnerImageBuild.objects.filter(
            image_definition_id=image_definition_id,
            runner_id=runner_id,
        )
        if organization_id is not None:
            queryset = queryset.filter(
                Q(image_definition__organization_id=organization_id)
                | Q(image_definition__organization__isnull=True)
            )
        return queryset.select_related("runner", "image_definition", "build_task").first()

    @staticmethod
    def get_by_id(runner_image_build_id: uuid.UUID) -> RunnerImageBuild | None:
        """Fetch one runner image build by primary key."""
        return RunnerImageBuild.objects.filter(
            id=runner_image_build_id
        ).select_related("runner", "image_definition", "build_task").first()

    @staticmethod
    def get_for_org(
        image_definition_id: uuid.UUID,
        runner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> RunnerImageBuild | None:
        """Fetch one runner image build scoped to an organization."""
        return RunnerImageBuildRepository.get(
            image_definition_id,
            runner_id,
            organization_id=organization_id,
        )

    @staticmethod
    def delete_for_org(
        image_definition_id: uuid.UUID,
        runner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> int:
        """Delete a runner image build scoped to an organization."""
        deleted, _ = RunnerImageBuild.objects.filter(
            image_definition_id=image_definition_id,
            runner_id=runner_id,
        ).filter(
            Q(image_definition__organization_id=organization_id)
            | Q(image_definition__organization__isnull=True)
        ).delete()
        return deleted
