"""
Database models for the runners app.

These models represent the backend's source-of-truth for runners,
workspaces, sessions, and task correlation.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

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


class Runner(models.Model):
    """
    A registered runner instance that manages workspace containers.

    Runners connect via WebSocket and authenticate with an API token.
    The backend tracks their connection state and capabilities.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, blank=True, default="")
    api_token_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 hash of the runner's API token.",
    )
    available_runtimes = models.JSONField(
        default=list,
        blank=True,
        help_text="List of runtime types this runner supports (e.g. ['docker', 'qemu']).",
    )
    qemu_min_vcpus = models.PositiveSmallIntegerField(default=1)
    qemu_max_vcpus = models.PositiveSmallIntegerField(default=8)
    qemu_default_vcpus = models.PositiveSmallIntegerField(default=2)
    qemu_min_memory_mb = models.PositiveIntegerField(default=1024)
    qemu_max_memory_mb = models.PositiveIntegerField(default=16384)
    qemu_default_memory_mb = models.PositiveIntegerField(default=4096)
    qemu_min_disk_size_gb = models.PositiveIntegerField(default=20)
    qemu_max_disk_size_gb = models.PositiveIntegerField(default=200)
    qemu_default_disk_size_gb = models.PositiveIntegerField(default=50)
    qemu_max_active_vcpus = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional cap for total vCPUs across active (running) QEMU workspaces.",
    )
    qemu_max_active_memory_mb = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional cap for total RAM (MiB) across active QEMU workspaces.",
    )
    qemu_max_active_disk_size_gb = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional cap for total disk size (GiB) across active QEMU workspaces.",
    )
    status = models.CharField(
        max_length=20,
        choices=RunnerStatus.choices,
        default=RunnerStatus.OFFLINE,
    )
    sid = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Socket.IO session ID for sending targeted messages.",
    )
    connected_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the last heartbeat received from this runner.",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="runners",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_runner"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        label = self.name or str(self.id)[:8]
        return f"Runner({label}, {self.status})"

    @property
    def is_online(self) -> bool:
        return self.status == RunnerStatus.ONLINE


class Workspace(models.Model):
    """
    A workspace managed by a runner — maps to a Docker container or QEMU VM.

    The backend is the source of truth for workspace state.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(
        Runner,
        on_delete=models.CASCADE,
        related_name="workspaces",
    )
    runtime_type = models.CharField(
        max_length=20,
        choices=RuntimeType.choices,
        default=RuntimeType.DOCKER,
        help_text="Virtualisation backend: 'docker' or 'qemu'.",
    )
    status = models.CharField(
        max_length=20,
        choices=WorkspaceStatus.choices,
        default=WorkspaceStatus.CREATING,
    )
    active_operation = models.CharField(
        max_length=32,
        choices=WorkspaceOperation.choices,
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255, default="", blank=True)
    qemu_vcpus = models.PositiveSmallIntegerField(null=True, blank=True)
    qemu_memory_mb = models.PositiveIntegerField(null=True, blank=True)
    qemu_disk_size_gb = models.PositiveIntegerField(null=True, blank=True)
    base_image_instance = models.ForeignKey(
        "runners.ImageInstance",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dependent_workspaces",
        help_text=(
            "The concrete runtime image this workspace was created from. "
            "Legacy workspaces created before image-instance tracking may be null."
        ),
    )
    credentials = models.ManyToManyField(
        "credentials.Credential",
        blank=True,
        related_name="workspaces",
        help_text=(
            "Credentials currently attached to this workspace. These are used "
            "when checking agent availability and when injecting env vars or "
            "SSH keys into the workspace runtime."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspaces",
    )
    last_activity_at = models.DateTimeField(
        default=timezone.now,
        help_text="Timestamp of the most recent user- or session-driven activity.",
    )
    delete_requested_at = models.DateTimeField(null=True, blank=True)
    delete_confirmed_at = models.DateTimeField(null=True, blank=True)
    delete_last_error = models.TextField(blank=True, default="")
    delete_attempt_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_workspace"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Workspace({self.name}, {self.status})"


class Chat(models.Model):
    """
    A conversation thread within a workspace.

    Groups related sessions (prompt/response pairs) together.
    Agents that support multi-chat allow users to create and switch
    between multiple chats within one workspace.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="chats",
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Display name for the chat (auto-set from first prompt if empty).",
    )
    agent_definition = models.ForeignKey(
        "runners.AgentDefinition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chats",
        help_text="The selected agent definition for this chat.",
    )
    agent_type = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Display snapshot of the selected agent name.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_chat"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        label = self.name or str(self.id)[:8]
        return f"Chat({label})"


class Session(models.Model):
    """
    A prompt/response session within a chat.

    Tracks the prompt text, streaming output, and completion status.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(
        Chat,
        on_delete=models.CASCADE,
        related_name="sessions",
    )
    prompt = models.TextField()
    agent_model = models.CharField(max_length=128, blank=True, default="")
    agent_options = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Selected agent options for this session as a key/value dict. "
            "Example: {\"model\": \"claude-opus-4.6\", \"permission_mode\": \"plan\"}"
        ),
    )
    output = models.TextField(blank=True, default="")
    error_message = models.TextField(null=True, blank=True, default=None)
    status = models.CharField(
        max_length=20,
        choices=SessionStatus.choices,
        default=SessionStatus.PENDING,
    )
    read_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user opened this completed/failed session.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "runners_session"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        preview = self.prompt[:40] + "..." if len(self.prompt) > 40 else self.prompt
        return f"Session({str(self.id)[:8]}, {preview})"


class Task(models.Model):
    """
    Correlates a backend command with a runner response.

    Every operation dispatched to a runner (create workspace, run prompt, etc.)
    creates a Task record. The runner references the task_id in its response
    events so the backend can match results to requests.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(
        Runner,
        on_delete=models.CASCADE,
        related_name="tasks",
    )
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tasks",
    )
    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tasks",
    )
    type = models.CharField(max_length=40, choices=TaskType.choices)
    status = models.CharField(
        max_length=20,
        choices=TaskStatus.choices,
        default=TaskStatus.PENDING,
    )
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "runners_task"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Task({str(self.id)[:8]}, {self.type}, {self.status})"


class AgentDefinition(models.Model):
    """
    DB-managed definition of a coding agent.

    Each record describes a coding tool (e.g. GitHub Copilot CLI) and its
    associated commands for workspace configuration and prompt execution.

    Standard definitions have ``organization=None`` and are read-only.
    Organization-specific definitions are owned by an org and can be modified
    by org admins.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="agent_definitions",
        help_text=(
            "The organization that owns this agent definition. "
            "Null means this is a standard/global definition."
        ),
    )
    name = models.CharField(
        max_length=64,
        help_text="Short identifier (e.g. 'copilot'). Unique globally for standard definitions, unique per org for org-specific ones.",
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Human-readable description of the agent.",
    )
    available_options = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "List of selectable options for this agent. Each entry is a dict with "
            "'key', 'label', 'choices' (list of strings), and 'default' (string). "
            "Example: [{\"key\": \"permission_mode\", \"label\": \"Permission Mode\", "
            "\"choices\": [\"default\", \"plan\"], \"default\": \"default\"}]"
        ),
    )
    default_env = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Environment variables injected into every run command for this agent. "
            "Defined as a key/value dict. Example: {\"ANTHROPIC_API_KEY\": \"sk-...\"}. "
            "Per-command env vars in AgentCommand.env take precedence over these defaults."
        ),
    )
    supports_multi_chat = models.BooleanField(
        default=False,
        help_text=(
            "Whether this agent supports multiple chat threads per workspace. "
            "When True, a {chat_id} placeholder in run command args is resolved."
        ),
    )
    required_credential_services = models.ManyToManyField(
        "credentials.CredentialService",
        blank=True,
        related_name="agents",
        help_text=(
            "Credential services required to use this agent. "
            "Agents can only be selected for workspaces that already include all "
            "of these credential services."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_agent_definition"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(organization__isnull=True),
                name="unique_standard_agent_name",
            ),
            models.UniqueConstraint(
                fields=["name", "organization"],
                condition=models.Q(organization__isnull=False),
                name="unique_org_agent_name",
            ),
        ]

    @property
    def is_standard(self) -> bool:
        """Return True when this is a global/standard definition."""
        return self.organization_id is None

    def __str__(self) -> str:
        if self.organization_id:
            return f"AgentDefinition({self.name}, org={self.organization_id})"
        return f"AgentDefinition({self.name})"



class OrgAgentDefinitionActivation(models.Model):
    """
    Controls which agent definitions are active for an organization.

    Both standard (organization=None) and org-specific agent definitions
    can be activated or deactivated per organization. When a new organization
    is created, all existing standard definitions are activated by default.

    Later this model can be extended to activate agents for specific groups
    or roles within the organization.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="agent_activations",
    )
    agent_definition = models.ForeignKey(
        AgentDefinition,
        on_delete=models.CASCADE,
        related_name="org_activations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "runners_org_agent_activation"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "agent_definition"],
                name="unique_org_agent_activation",
            ),
        ]

    def __str__(self) -> str:
        return f"OrgAgentActivation(org={self.organization_id}, agent={self.agent_definition_id})"


class AgentCommand(models.Model):
    """
    A command associated with an agent definition.

    Commands belong to one of two phases:
    - ``configure``: Run once after workspace creation (may have many).
    - ``run``: Template for executing a prompt (exactly one per agent).

    Template variables in ``args`` and ``workdir`` fields:
    - ``{prompt}`` — replaced with the user's prompt text.
    - ``{workdir}`` — replaced with the workspace working directory.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        AgentDefinition,
        on_delete=models.CASCADE,
        related_name="commands",
    )
    phase = models.CharField(
        max_length=20,
        choices=AgentCommandPhase.choices,
        help_text="When this command is executed: 'configure' or 'run'.",
    )
    args = models.JSONField(
        help_text=(
            "Command arguments as a list of strings. "
            "May contain {prompt} and {workdir} placeholders."
        ),
    )
    workdir = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text="Working directory. May contain {workdir} placeholder.",
    )
    env = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra environment variables for this command.",
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Human-readable description of what this command does.",
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Execution order within the same phase.",
    )

    class Meta:
        db_table = "runners_agent_command"
        ordering = ["agent", "phase", "order"]
        constraints = [
            models.UniqueConstraint(
                fields=["agent", "phase"],
                condition=models.Q(phase="run"),
                name="unique_run_command_per_agent",
            ),
        ]

    def __str__(self) -> str:
        return f"AgentCommand({self.agent.name}, {self.phase}, order={self.order})"


class AgentDefinitionCredentialRelation(models.Model):
    """
    Links a CredentialService to an AgentDefinition with optional defaults.

    When a workspace is created with a credential belonging to this service,
    the ``default_env`` and ``commands`` defined here are applied in addition
    to (and before) those on the AgentDefinition itself.

    This allows credential-specific setup steps (e.g. authenticating a CLI
    tool with the injected token) to be encapsulated here so that users only
    need to attach the credential service — everything else is automatic.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent_definition = models.ForeignKey(
        AgentDefinition,
        on_delete=models.CASCADE,
        related_name="credential_relations",
    )
    credential_service = models.ForeignKey(
        "credentials.CredentialService",
        on_delete=models.CASCADE,
        related_name="agent_relations",
    )
    default_env = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Environment variables to inject when this credential service is present. "
            "Applied before the AgentDefinition's own default_env."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_agent_credential_relation"
        ordering = ["agent_definition", "credential_service"]
        constraints = [
            models.UniqueConstraint(
                fields=["agent_definition", "credential_service"],
                name="unique_agent_credential_relation",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"AgentDefinitionCredentialRelation("
            f"agent={self.agent_definition_id}, "
            f"service={self.credential_service_id})"
        )


class AgentCredentialRelationCommand(models.Model):
    """
    A command associated with an AgentDefinitionCredentialRelation.

    Executed when the linked credential service is present in the workspace,
    before the AgentDefinition's own commands for the same phase.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    relation = models.ForeignKey(
        AgentDefinitionCredentialRelation,
        on_delete=models.CASCADE,
        related_name="commands",
    )
    phase = models.CharField(
        max_length=20,
        choices=AgentCommandPhase.choices,
        help_text="When this command is executed: 'configure' or 'run'.",
    )
    args = models.JSONField(
        help_text=(
            "Command arguments as a list of strings. "
            "May contain {prompt} and {workdir} placeholders."
        ),
    )
    workdir = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text="Working directory. May contain {workdir} placeholder.",
    )
    env = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra environment variables for this command.",
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Human-readable description of what this command does.",
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Execution order within the same phase.",
    )

    class Meta:
        db_table = "runners_agent_credential_relation_command"
        ordering = ["relation", "phase", "order"]

    def __str__(self) -> str:
        return (
            f"AgentCredentialRelationCommand("
            f"relation={self.relation_id}, {self.phase}, order={self.order})"
        )


class RunnerSystemMetrics(models.Model):
    """
    Point-in-time system resource snapshot reported by a runner.

    Logged every minute by each runner. The ``timestamp`` field is the
    primary lookup key — it is indexed (via ``db_index=True``) so that
    range queries (e.g. "last N minutes") remain efficient.
    """

    runner = models.ForeignKey(
        Runner,
        on_delete=models.CASCADE,
        related_name="system_metrics",
    )
    timestamp = models.DateTimeField(
        db_index=True,
        help_text="UTC timestamp when these metrics were recorded.",
    )
    cpu_usage_percent = models.FloatField(
        help_text="Mean CPU utilisation across all cores (0–100).",
    )
    ram_used_bytes = models.BigIntegerField(
        help_text="RAM currently in use (bytes).",
    )
    ram_total_bytes = models.BigIntegerField(
        help_text="Total installed RAM (bytes).",
    )
    disk_used_bytes = models.BigIntegerField(
        help_text="Disk space used on the root filesystem (bytes).",
    )
    disk_total_bytes = models.BigIntegerField(
        help_text="Total disk capacity of the root filesystem (bytes).",
    )
    vm_metrics = models.JSONField(
        null=True,
        blank=True,
        help_text="Per-VM usage metrics keyed by workspace ID.",
    )

    class Meta:
        db_table = "runners_system_metrics"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["runner", "-timestamp"], name="runner_metrics_ts_idx"),
        ]

    def __str__(self) -> str:
        return f"RunnerSystemMetrics(runner={self.runner_id}, ts={self.timestamp})"


class ImageDefinition(models.Model):
    """DB-managed definition of a buildable workspace image."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DEACTIVATED = "deactivated", "Deactivated"
        PENDING_DELETION = "pending_deletion", "Pending Deletion"
        DELETING = "deleting", "Deleting"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="image_definitions",
        help_text=(
            "The organization that owns this image definition. "
            "Null means this is a standard/global definition."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_image_definitions",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    runtime_type = models.CharField(
        max_length=20,
        choices=RuntimeType.choices,
        default=RuntimeType.DOCKER,
    )
    base_distro = models.CharField(max_length=255, default="ubuntu:22.04")
    packages = models.JSONField(default=list, blank=True)
    env_vars = models.JSONField(default=dict, blank=True)
    custom_dockerfile = models.TextField(blank=True, default="")
    custom_init_script = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        help_text="Lifecycle status of the image definition.",
    )
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_image_definition"
        ordering = ["name", "-updated_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(organization__isnull=True),
                name="unique_standard_image_definition_name",
            ),
            models.UniqueConstraint(
                fields=["name", "organization"],
                condition=models.Q(organization__isnull=False),
                name="unique_org_image_definition_name",
            ),
        ]

    @property
    def is_standard(self) -> bool:
        """Return True when this is a global/standard definition."""
        return self.organization_id is None

    def __str__(self) -> str:
        if self.organization_id:
            return (
                f"ImageDefinition({self.name}, runtime={self.runtime_type}, "
                f"org={self.organization_id})"
            )
        return f"ImageDefinition({self.name}, runtime={self.runtime_type})"


class ImageBuildJob(models.Model):
    """Per-runner build/activation status for an image definition."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        BUILDING = "building", "Building"
        ACTIVE = "active", "Active"
        FAILED = "failed", "Failed"
        DEACTIVATED = "deactivated", "Deactivated"
        PENDING_DELETION = "pending_deletion", "Pending Deletion"
        DELETING = "deleting", "Deleting"
        DELETED = "deleted", "Deleted"
        DELETE_FAILED = "delete_failed", "Delete Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    image_definition = models.ForeignKey(
        ImageDefinition,
        on_delete=models.CASCADE,
        related_name="runner_builds",
    )
    runner = models.ForeignKey(
        Runner,
        on_delete=models.CASCADE,
        related_name="image_builds",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    build_log = models.TextField(blank=True, default="")
    build_task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="build_jobs",
    )
    deleting_task_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="Task ID of the delete task while cleanup is pending.",
    )
    delete_requested_at = models.DateTimeField(null=True, blank=True)
    delete_started_at = models.DateTimeField(null=True, blank=True)
    delete_confirmed_at = models.DateTimeField(null=True, blank=True)
    delete_last_error = models.TextField(blank=True, default="")
    delete_attempt_count = models.PositiveIntegerField(default=0)
    built_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_build_job"
        ordering = ["-updated_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["image_definition", "runner"],
                name="uniq_build_job_definition_runner",
            )
        ]

    def __str__(self) -> str:
        return (
            "ImageBuildJob("
            f"definition={self.image_definition_id}, runner={self.runner_id}, status={self.status})"
        )


class ImageInstance(models.Model):
    """Concrete runnable image instance tracked independently from definitions."""

    class OriginType(models.TextChoices):
        DEFINITION_BUILD = "definition_build", "Definition Build"
        WORKSPACE_CAPTURE = "workspace_capture", "Workspace Capture"

    class Status(models.TextChoices):
        BUILDING = "building", "Building"
        CAPTURING = "capturing", "Capturing"
        READY = "ready", "Ready"
        RETIRED = "retired", "Retired"
        PENDING_DELETION = "pending_deletion", "Pending Deletion"
        DELETING = "deleting", "Deleting"
        DELETED = "deleted", "Deleted"
        DELETE_FAILED = "delete_failed", "Delete Failed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(
        Runner,
        on_delete=models.CASCADE,
        related_name="image_instances",
    )
    runtime_type = models.CharField(
        max_length=20,
        choices=RuntimeType.choices,
        default=RuntimeType.DOCKER,
    )
    origin_type = models.CharField(
        max_length=32,
        choices=OriginType.choices,
        default=OriginType.WORKSPACE_CAPTURE,
    )
    origin_definition = models.ForeignKey(
        ImageDefinition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="image_instances",
    )
    origin_workspace = models.ForeignKey(
        Workspace,
        on_delete=models.SET_NULL,
        related_name="captured_image_instances",
        null=True,
        blank=True,
    )
    build_job = models.OneToOneField(
        ImageBuildJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="image_instance",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="image_instances",
        null=True,
        blank=True,
        help_text="The user who created this image instance.",
    )
    runner_ref = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=(
            "Concrete runtime reference on the runner, e.g. a Docker image tag "
            "or QCOW2 path."
        ),
    )
    name = models.CharField(
        max_length=255,
        help_text="Human-readable image instance name.",
    )
    size_bytes = models.BigIntegerField(
        default=0,
        help_text="Image instance size in bytes.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.READY,
        help_text="Lifecycle status of the image instance.",
    )
    creating_task_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="Task ID of the creation/build task.",
    )
    deleting_task_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="Task ID of the delete task while cleanup is pending.",
    )
    delete_requested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When deletion was first requested.",
    )
    delete_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the runner began physical deletion.",
    )
    delete_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the runner confirmed deletion.",
    )
    delete_last_error = models.TextField(
        blank=True,
        default="",
        help_text="Last error message from a deletion attempt.",
    )
    delete_attempt_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of deletion attempts.",
    )
    credentials = models.ManyToManyField(
        "credentials.Credential",
        blank=True,
        related_name="image_instances",
        help_text=(
            "Credentials associated with this image instance. "
            "Workspace cloning must still supply credentials explicitly."
        ),
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_image_instance"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return (
            "ImageInstance("
            f"{self.name}, origin_type={self.origin_type}, status={self.status})"
        )
