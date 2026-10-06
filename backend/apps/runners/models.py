"""
Database models for the runners app.

These models represent the backend's source-of-truth for runners,
workspaces, and task correlation. Agent conversations live in the
harness app (``HarnessSession``/``HarnessMessage``/``HarnessPart``).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .enums import (
    ProcessStatus,
    RunnerStatus,
    RuntimeType,
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
    current_task = models.ForeignKey(
        "runners.Task",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_for_workspaces",
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
    desktop_width = models.PositiveIntegerField(
        default=1920,
        help_text=(
            "Fixed Xvnc framebuffer width in pixels. Applied the next "
            "time the desktop starts."
        ),
    )
    desktop_height = models.PositiveIntegerField(
        default=1080,
        help_text=(
            "Fixed Xvnc framebuffer height in pixels. Applied the next "
            "time the desktop starts."
        ),
    )
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
    pending_base_image_instance = models.ForeignKey(
        "runners.ImageInstance",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pending_workspaces",
        help_text=(
            "Target image version of an in-flight or failed recreate. It pins "
            "the version until the workspace has been provisioned from it."
        ),
    )
    repos = models.JSONField(
        default=list,
        db_default=models.Value([], output_field=models.JSONField()),
        blank=True,
        help_text="Repositories cloned on top of the base image when provisioning.",
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
    credentials_present = models.BooleanField(
        default=False,
        help_text=(
            "True if credential material is currently on the workspace disk. "
            "Set after a successful inject; cleared only after a controlled stop "
            "removes the secrets. External stops leave this true."
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
    delete_started_at = models.DateTimeField(null=True, blank=True)
    delete_confirmed_at = models.DateTimeField(null=True, blank=True)
    delete_last_error = models.TextField(blank=True, default="")
    delete_attempt_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_workspace"
        ordering = ["-created_at"]

    @property
    def intervention_required(self) -> bool:
        """A terminal task with a retained fence is explicit operator work, not busy."""
        return bool(self.current_task_id and self.current_task.status == "failed")

    @property
    def lifecycle_diagnostic(self) -> str:
        """Explain why a retained fence cannot be silently cleared."""
        return self.current_task.error if self.intervention_required else ""

    def __str__(self) -> str:
        return f"Workspace({self.name}, {self.status})"


class Task(models.Model):
    """
    Correlates a backend command with a runner response.

    Every operation dispatched to a runner (create workspace, lifecycle,
    terminal, harness RPC, etc.) creates a Task record. The runner
    references the task_id in its response events so the backend can
    match results to requests.
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


class WorkspaceProcess(models.Model):
    """
    A backend-tracked background process running inside a workspace.

    The process ``name`` is the identity of a persistent application
    within its workspace (unique per workspace, case-sensitive). Starting
    an existing name reuses the same row (stable id), bumps ``run_count``
    and writes a fresh log file — earlier logs are kept. The backend is
    the source of truth for process bookkeeping (status, exit code, pid,
    log path, run count). The runner owns the actual OS process and
    reports live state via ``harness:process_*`` RPC results and the
    per-workspace ``processes`` heartbeat payload. Log content stays
    decentralised as files inside the workspace
    (``.opencuria/processes/<id>.log``, ``<id>_r<run>.log`` for later
    runs); agents read them via file tools. Processes are
    workspace-bound: stopping or removing the workspace kills them.
    There is no auto-restart — every run is explicit (same name).

    Temporary processes (``kind=TEMP``) are session-scoped: they run at
    most until the owning harness run finishes. The harness cleanup hook
    stops all running temp processes of the finished session; the rows
    stay in the DB (finished, non-running). Temp names are unique per
    ``(workspace, name, session_id)`` so parallel sessions never collide.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="processes",
        help_text=(
            "Workspace this process runs in. "
            "Deleting the workspace deletes its processes."
        ),
    )
    name = models.CharField(
        max_length=255,
        default="",
        blank=False,
        help_text=(
            "Identity of this persistent application within its workspace. "
            "Unique per workspace (case-sensitive); restarts reuse the row."
        ),
    )
    run_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of starts of this named application.",
    )
    kind = models.CharField(
        max_length=20,
        default="persistent",
        help_text=(
            "persistent: workspace-global app (name unique per workspace). "
            "temp: session-scoped helper (name unique per workspace+session), "
            "stopped automatically when the owning harness run finishes."
        ),
    )
    command = models.TextField(
        help_text="Shell command the process was started with.",
    )
    workdir = models.CharField(max_length=1024, default="/workspace")
    pid = models.IntegerField(null=True, blank=True)
    log_path = models.CharField(max_length=1024, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=ProcessStatus.choices,
        default=ProcessStatus.RUNNING,
    )
    exit_code = models.IntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_processes",
    )
    session_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Agent run (harness session) that owns this process. "
            "Informational for persistent processes; required for temp "
            "processes (scope of the temp name + cleanup hook target)."
        ),
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_workspace_process"
        ordering = ["-started_at"]
        indexes = [
            models.Index(
                fields=["workspace", "session_id", "status"],
                name="process_ws_session_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                condition=models.Q(kind="persistent"),
                name="uniq_workspace_process_name",
            ),
            models.UniqueConstraint(
                fields=["workspace", "name", "session_id"],
                condition=models.Q(kind="temp"),
                name="uniq_workspace_temp_process_name",
            ),
        ]

    def __str__(self) -> str:
        return f"WorkspaceProcess({str(self.id)[:8]}, {self.status})"


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
        DELETE_FAILED = "delete_failed", "Delete Failed"

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
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        help_text="Lifecycle status of the image definition.",
    )
    deactivated_at = models.DateTimeField(null=True, blank=True)
    delete_requested_at = models.DateTimeField(null=True, blank=True)
    delete_started_at = models.DateTimeField(null=True, blank=True)
    delete_confirmed_at = models.DateTimeField(null=True, blank=True)
    delete_last_error = models.TextField(blank=True, default="")
    delete_attempt_count = models.PositiveIntegerField(default=0)
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
    def is_active(self) -> bool:
        """Compatibility view; lifecycle status is the single source of truth."""
        return self.status == self.Status.ACTIVE

    @is_active.setter
    def is_active(self, value: bool) -> None:
        if self.status not in {self.Status.ACTIVE, self.Status.DEACTIVATED}:
            from common.exceptions import ConflictError

            raise ConflictError("Image definition is being removed")
        self.status = self.Status.ACTIVE if value else self.Status.DEACTIVATED

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


class ImageRevision(models.Model):
    """Immutable recipe snapshot, including the exact input sent to the runner."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        ImageDefinition, on_delete=models.PROTECT, related_name="revisions"
    )
    digest = models.CharField(max_length=64)
    recipe = models.JSONField()
    rendered_input = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["definition", "digest"], name="unique_image_revision_digest"
            )
        ]

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            raise ValueError("Image revisions are immutable")
        super().save(*args, **kwargs)


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
    legacy_task_references = models.JSONField(default=dict, blank=True)
    current_generation = models.ForeignKey(
        "ImageInstance",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_assignments",
    )
    pending_generation = models.ForeignKey(
        "ImageInstance",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pending_assignments",
    )

    @property
    def image_instance(self):
        """Compatibility alias for the selected generation, not latest attempt."""
        return self.current_generation

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
    deleting_task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        db_column="deleting_task_id",
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


class CapturedImage(models.Model):
    """A versioned line of workspace captures owned by one user on one runner."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PENDING_DELETION = "pending_deletion", "Pending Deletion"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="captured_images",
    )
    runner = models.ForeignKey(
        Runner,
        on_delete=models.CASCADE,
        related_name="captured_images",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="captured_images",
    )
    name = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_captured_image"
        ordering = ["name", "-created_at"]

    def __str__(self) -> str:
        return f"CapturedImage({self.name}, status={self.status})"


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
    build_job = models.ForeignKey(
        ImageBuildJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generations",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="image_instances",
        null=True,
        blank=True,
        help_text="The user who created this image instance.",
    )
    revision = models.ForeignKey(
        ImageRevision,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="generations",
    )
    captured_image = models.ForeignKey(
        CapturedImage,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="versions",
    )
    generation = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Version number within the image line (build job or capture).",
    )
    message = models.TextField(
        blank=True,
        default="",
        db_default="",
        help_text="Short description of what changed in this version.",
    )
    min_disk_size_gb = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Smallest workspace disk (GiB) that can hold this captured version.",
    )
    is_legacy = models.BooleanField(default=False)
    legacy_task_references = models.JSONField(default=dict, blank=True)

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
        null=True,
        blank=True,
        default=None,
        help_text="Image instance size in bytes.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.READY,
        help_text="Lifecycle status of the image instance.",
    )
    creating_task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        db_column="creating_task_id",
    )
    deleting_task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        db_column="deleting_task_id",
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
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_image_instance"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(size_bytes__isnull=True)
                | models.Q(size_bytes__gte=0),
                name="image_size_nonnegative",
            ),
            models.UniqueConstraint(
                fields=["build_job", "generation"], name="unique_image_generation"
            ),
            models.UniqueConstraint(
                fields=["captured_image", "generation"],
                name="unique_captured_image_version",
            ),
            models.CheckConstraint(
                condition=models.Q(is_legacy=True)
                | (
                    models.Q(
                        origin_type="definition_build",
                        origin_definition__isnull=False,
                        build_job__isnull=False,
                        revision__isnull=False,
                        generation__gte=1,
                        generation__isnull=False,
                        origin_workspace__isnull=True,
                        captured_image__isnull=True,
                    )
                )
                | (
                    models.Q(
                        origin_type="workspace_capture",
                        origin_definition__isnull=True,
                        build_job__isnull=True,
                        revision__isnull=True,
                        captured_image__isnull=False,
                        generation__gte=1,
                        generation__isnull=False,
                        runtime_type="qemu",
                    )
                ),
                name="image_origin_valid",
            ),
        ]

    def save(self, *args, **kwargs) -> None:
        """Generation identity and build target are write-once."""
        if not self._state.adding:
            identity_fields = (
                "runner_id",
                "runtime_type",
                "origin_type",
                "origin_definition_id",
                "build_job_id",
                "captured_image_id",
                "revision_id",
                "generation",
                "is_legacy",
            )
            previous = (
                type(self)
                .objects.filter(id=self.id)
                .values(*identity_fields, "runner_ref")
                .first()
            )
            if previous and not previous["is_legacy"]:
                if any(previous[key] != getattr(self, key) for key in identity_fields):
                    raise ValueError("Image generation identity is immutable")
                if previous["runner_ref"] and previous["runner_ref"] != self.runner_ref:
                    raise ValueError("Image generation target is immutable")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            "ImageInstance("
            f"{self.name}, origin_type={self.origin_type}, status={self.status})"
        )


class LifecycleCommand(models.Model):
    """One durable intent/outbox per Task, not a second execution state machine."""

    task = models.OneToOneField(Task, primary_key=True, on_delete=models.CASCADE)
    event = models.CharField(max_length=80, blank=True)
    payload = models.JSONField(default=dict)
    target = models.CharField(max_length=512, blank=True)
    attempt = models.PositiveIntegerField(default=1)
    deliveries = models.PositiveIntegerField(default=0)
    phase = models.CharField(max_length=40, default="intent")
    heartbeat_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deadline_at = models.DateTimeField()
    next_delivery_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(default=timezone.now)


class InventorySnapshot(models.Model):
    """Authenticated observation; partial scans never replace complete evidence."""

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE)
    session = models.CharField(max_length=255)
    epoch = models.UUIDField()
    sequence = models.PositiveBigIntegerField()
    received_at = models.DateTimeField(default=timezone.now)
    complete = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["runner", "session", "epoch", "sequence"],
                name="inventory_unique_sequence",
            )
        ]


class InventoryRuntime(models.Model):
    snapshot = models.ForeignKey(
        InventorySnapshot, on_delete=models.CASCADE, related_name="runtimes"
    )
    runtime_type = models.CharField(max_length=20)
    collected_at = models.DateTimeField()
    complete = models.BooleanField(default=False)
    errors = models.JSONField(default=list)
    filesystems = models.JSONField(default=list)
    foreign_resource_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "runtime_type"], name="inventory_unique_runtime"
            )
        ]


class InventoryResource(models.Model):
    runtime = models.ForeignKey(
        InventoryRuntime, on_delete=models.CASCADE, related_name="resources"
    )
    physical_id = models.TextField()
    kind = models.CharField(max_length=40)
    managed = models.BooleanField(default=False)
    state = models.CharField(max_length=40, default="unknown")
    allocated_bytes = models.PositiveBigIntegerField(null=True)
    logical_bytes = models.PositiveBigIntegerField(null=True)
    virtual_bytes = models.PositiveBigIntegerField(null=True)
    shared_bytes = models.PositiveBigIntegerField(null=True)
    reclaimable_bytes = models.PositiveBigIntegerField(null=True)
    provenance = models.TextField(blank=True)
    metadata = models.JSONField(default=dict)
    image = models.ForeignKey(ImageInstance, null=True, on_delete=models.SET_NULL)
    workspace = models.ForeignKey(Workspace, null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["runtime", "physical_id"], name="inventory_unique_resource"
            )
        ]


class InventoryAlias(models.Model):
    resource = models.ForeignKey(
        InventoryResource, on_delete=models.CASCADE, related_name="aliases"
    )
    reference = models.TextField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "reference"], name="inventory_unique_alias"
            )
        ]


class InventoryEdge(models.Model):
    """Missing dependency endpoints are stored as unknown resources, not discarded."""

    source = models.ForeignKey(
        InventoryResource, on_delete=models.CASCADE, related_name="dependencies"
    )
    target = models.ForeignKey(
        InventoryResource, on_delete=models.CASCADE, related_name="dependents"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "target"], name="inventory_unique_edge"
            )
        ]


class InventoryRefresh(models.Model):
    """Coalesced durable full-scan request, satisfied only by a newer complete scan."""

    runner = models.OneToOneField(Runner, primary_key=True, on_delete=models.CASCADE)
    requested_at = models.DateTimeField(default=timezone.now)
    next_delivery_at = models.DateTimeField(default=timezone.now)
    fulfilled_at = models.DateTimeField(null=True)


class CaptureRequest(models.Model):
    """Durable QEMU stop/capture/resume orchestration, separate from Task execution."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    image = models.OneToOneField(ImageInstance, on_delete=models.PROTECT)
    phase = models.CharField(max_length=32, default="stop")
    prior_running = models.BooleanField(default=False)
    resume_suppressed = models.BooleanField(default=False)
    child = models.ForeignKey(Task, null=True, on_delete=models.PROTECT)
    diagnostic = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class WorkspaceRecreateRequest(models.Model):
    """Durable remove/create/stop orchestration that keeps the workspace identity.

    Used for reset (same version), update (latest version) and the automatic
    replacement after a capture. The target version lives on
    ``Workspace.pending_base_image_instance`` until the old runtime is removed.
    """

    class Reason(models.TextChoices):
        RESET = "reset", "Reset"
        UPDATE = "update", "Update"
        CAPTURE = "capture", "Capture"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.PROTECT, related_name="recreate_requests"
    )
    reason = models.CharField(max_length=16, choices=Reason.choices)
    phase = models.CharField(max_length=16, default="remove")
    final_running = models.BooleanField(default=True)
    child = models.ForeignKey(Task, null=True, on_delete=models.PROTECT)
    diagnostic = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_workspace_recreate_request"
        ordering = ["-created_at"]


class ImageDeletionRequest(models.Model):
    """Durable approval and tombstone; child execution remains Task-owned."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    target_type = models.CharField(max_length=20)
    target_id = models.UUIDField()
    mode = models.CharField(max_length=12, default="deferred")
    phase = models.CharField(max_length=32, default="waiting_inventory")
    diagnostic = models.TextField(blank=True)
    fingerprint = models.CharField(max_length=64, blank=True)
    approval = models.JSONField(default=dict)
    previous = models.JSONField(default=dict)
    children = models.JSONField(default=dict)
    released_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "target_type", "target_id"],
                condition=~models.Q(phase__in=["completed", "cancelled"]),
                name="unique_live_image_deletion",
            )
        ]
