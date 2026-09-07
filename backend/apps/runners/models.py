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

    The backend is the source of truth for process bookkeeping (status,
    exit code, pid, log path). The runner owns the actual OS process and
    reports live state via ``harness:process_*`` RPC results and the
    per-workspace ``processes`` heartbeat payload. Log content stays
    decentralised as a file inside the workspace
    (``.opencuria/processes/<id>.log``); agents read it via file tools.
    Processes are workspace-bound: stopping or removing the workspace
    kills them. There is no auto-restart.
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
    name = models.CharField(max_length=255, blank=True, default="")
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
        help_text="Agent loop that started this process (informational, no FK).",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runners_workspace_process"
        ordering = ["-started_at"]

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
    is_active = models.BooleanField(default=True)
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
