"""
Pydantic schemas for the runners REST API.

Separated into input (In) and output (Out) schemas for clarity.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ninja import Schema
from pydantic import field_validator

from .desktop import (
    DEFAULT_DESKTOP_HEIGHT,
    DEFAULT_DESKTOP_WIDTH,
    validate_desktop_dimension,
)

# ---------------------------------------------------------------------------
# Runner schemas
# ---------------------------------------------------------------------------


class RunnerOut(Schema):
    """Response schema for a runner."""

    id: uuid.UUID
    name: str
    status: str
    available_runtimes: list[str] = []
    organization_id: uuid.UUID
    connected_at: datetime | None
    disconnected_at: datetime | None
    qemu_min_vcpus: int
    qemu_max_vcpus: int
    qemu_default_vcpus: int
    qemu_min_memory_mb: int
    qemu_max_memory_mb: int
    qemu_default_memory_mb: int
    qemu_min_disk_size_gb: int
    qemu_max_disk_size_gb: int
    qemu_default_disk_size_gb: int
    qemu_max_active_vcpus: int | None
    qemu_max_active_memory_mb: int | None
    qemu_max_active_disk_size_gb: int | None
    created_at: datetime
    updated_at: datetime


class RunnerSystemMetricsOut(Schema):
    """Response schema for a runner system metrics snapshot."""

    runner_id: uuid.UUID
    timestamp: datetime
    cpu_usage_percent: float
    ram_used_bytes: int
    ram_total_bytes: int
    disk_used_bytes: int
    disk_total_bytes: int
    vm_metrics: dict[str, Any] | None = None


class RunnerCreateIn(Schema):
    """Request schema for registering a new runner."""

    name: str = ""


class RunnerCreateOut(Schema):
    """Response schema for runner creation — includes the plaintext API token."""

    id: uuid.UUID
    name: str
    api_token: str


class RunnerUpdateIn(Schema):
    """Request schema for updating runner QEMU resource limits/defaults."""

    qemu_min_vcpus: int | None = None
    qemu_max_vcpus: int | None = None
    qemu_default_vcpus: int | None = None
    qemu_min_memory_mb: int | None = None
    qemu_max_memory_mb: int | None = None
    qemu_default_memory_mb: int | None = None
    qemu_min_disk_size_gb: int | None = None
    qemu_max_disk_size_gb: int | None = None
    qemu_default_disk_size_gb: int | None = None
    qemu_max_active_vcpus: int | None = None
    qemu_max_active_memory_mb: int | None = None
    qemu_max_active_disk_size_gb: int | None = None


# ---------------------------------------------------------------------------
# Workspace schemas
# ---------------------------------------------------------------------------


class WorkspaceOut(Schema):
    """Response schema for a workspace."""

    id: uuid.UUID
    runner_id: uuid.UUID
    status: str
    active_operation: str | None = None
    name: str
    runtime_type: str = "docker"
    qemu_vcpus: int | None = None
    qemu_memory_mb: int | None = None
    qemu_disk_size_gb: int | None = None
    desktop_width: int = DEFAULT_DESKTOP_WIDTH
    desktop_height: int = DEFAULT_DESKTOP_HEIGHT
    created_by_id: int
    last_activity_at: datetime
    auto_stop_timeout_minutes: int | None = None
    auto_stop_at: datetime | None = None
    delete_requested_at: datetime | None = None
    delete_started_at: datetime | None = None
    delete_confirmed_at: datetime | None = None
    delete_last_error: str = ""
    delete_attempt_count: int = 0
    created_at: datetime
    updated_at: datetime
    has_active_session: bool = False
    runner_online: bool = False
    credential_ids: list[uuid.UUID] = []
    credentials_present: bool = False
    base_image_name: str | None = None


class WorkspaceCreateIn(Schema):
    """Request schema for creating a workspace."""

    name: str
    repos: list[str] = []
    runtime_type: str = "docker"
    credential_ids: list[uuid.UUID] = []
    runner_id: uuid.UUID | None = None
    qemu_vcpus: int | None = None
    qemu_memory_mb: int | None = None
    qemu_disk_size_gb: int | None = None
    desktop_width: int = DEFAULT_DESKTOP_WIDTH
    desktop_height: int = DEFAULT_DESKTOP_HEIGHT
    image_artifact_id: uuid.UUID

    @field_validator("desktop_width")
    @classmethod
    def _validate_desktop_width(cls, value: int) -> int:
        return validate_desktop_dimension(value, kind="width")

    @field_validator("desktop_height")
    @classmethod
    def _validate_desktop_height(cls, value: int) -> int:
        return validate_desktop_dimension(value, kind="height")


class WorkspaceUpdateIn(Schema):
    """Request schema for updating workspace metadata."""

    name: str | None = None
    credential_ids: list[uuid.UUID] | None = None
    qemu_vcpus: int | None = None
    qemu_memory_mb: int | None = None
    qemu_disk_size_gb: int | None = None
    desktop_width: int | None = None
    desktop_height: int | None = None

    @field_validator("desktop_width")
    @classmethod
    def _validate_desktop_width(cls, value: int | None) -> int | None:
        if value is None:
            return value
        return validate_desktop_dimension(value, kind="width")

    @field_validator("desktop_height")
    @classmethod
    def _validate_desktop_height(cls, value: int | None) -> int | None:
        if value is None:
            return value
        return validate_desktop_dimension(value, kind="height")


class WorkspaceUpdateOut(Schema):
    """Response schema for workspace metadata updates."""

    id: uuid.UUID
    name: str
    updated_at: datetime
    active_operation: str | None = None
    credential_ids: list[uuid.UUID] = []
    credentials_present: bool = False
    qemu_vcpus: int | None = None
    qemu_memory_mb: int | None = None
    qemu_disk_size_gb: int | None = None
    desktop_width: int = DEFAULT_DESKTOP_WIDTH
    desktop_height: int = DEFAULT_DESKTOP_HEIGHT


class WorkspaceCreateOut(Schema):
    """Response schema after workspace creation is dispatched."""

    workspace_id: uuid.UUID
    task_id: uuid.UUID
    status: str


# ---------------------------------------------------------------------------
# Task schemas
# ---------------------------------------------------------------------------


class TaskOut(Schema):
    """Response schema for a task."""

    id: uuid.UUID
    runner_id: uuid.UUID
    workspace_id: uuid.UUID | None
    type: str
    status: str
    error: str
    created_at: datetime
    completed_at: datetime | None


# ---------------------------------------------------------------------------
# Terminal schemas
# ---------------------------------------------------------------------------


class TerminalStartIn(Schema):
    """Request schema for starting an interactive terminal."""

    cols: int = 80
    rows: int = 24


class TerminalStartOut(Schema):
    """Response schema after terminal start is dispatched."""

    task_id: uuid.UUID


# ---------------------------------------------------------------------------
# Desktop session schemas
# ---------------------------------------------------------------------------


class DesktopStartOut(Schema):
    """Response schema after desktop start is dispatched."""

    task_id: uuid.UUID


class DesktopStopOut(Schema):
    """Response schema after desktop stop is dispatched."""

    task_id: uuid.UUID


class DesktopStatusOut(Schema):
    """Response schema for desktop session status check."""

    active: bool
    proxy_url: str | None = None
    viewer_held: bool = False
    computer_use_active: bool = False


class DesktopTakeControlOut(Schema):
    """Response schema after taking control of the desktop from computer-use."""

    aborted_session_ids: list[uuid.UUID]


class DesktopClipboardWriteIn(Schema):
    """Request schema for writing plain text into the VM clipboard."""

    text: str


class DesktopClipboardReadOut(Schema):
    """Response schema for reading plain text from the VM clipboard."""

    text: str


# ---------------------------------------------------------------------------
# Background process schemas
# ---------------------------------------------------------------------------


class ProcessStartIn(Schema):
    """Request schema for starting a background process."""

    command: str
    workdir: str = "/workspace"
    env: dict[str, str] = {}
    name: str = ""


class ProcessOut(Schema):
    """Response schema for a workspace background process."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    command: str
    workdir: str
    pid: int | None = None
    log_path: str = ""
    status: str
    exit_code: int | None = None
    started_at: datetime
    ended_at: datetime | None = None
    updated_at: datetime


# ---------------------------------------------------------------------------
# Error schemas
# ---------------------------------------------------------------------------


class ErrorOut(Schema):
    """Standard error response."""

    detail: str
    code: str = "error"


# ---------------------------------------------------------------------------
# Image schemas
# ---------------------------------------------------------------------------


class ImageArtifactOut(Schema):
    """Response schema for a concrete image artifact."""

    id: uuid.UUID
    source_workspace_id: uuid.UUID | None = None
    runner_artifact_id: str
    name: str
    size_bytes: int
    status: str
    artifact_kind: str = "captured"
    build_job_id: uuid.UUID | None = None
    source_definition_name: str | None = None
    source_runner_id: uuid.UUID | None = None
    runtime_type: str | None = None
    is_deactivated: bool = False
    source_runner_online: bool = False
    delete_requested_at: datetime | None = None
    delete_confirmed_at: datetime | None = None
    delete_last_error: str = ""
    created_at: datetime
    created_by_id: int | None = None


class ImageArtifactUpdateIn(Schema):
    """Request schema for renaming an image artifact."""

    name: str


class ImageArtifactCreateIn(Schema):
    """Request schema for creating an image artifact."""

    name: str
    workspace_id: uuid.UUID | None = None


class ImageArtifactCreateOut(Schema):
    """Response schema after artifact creation is dispatched."""

    task_id: uuid.UUID
    workspace_id: uuid.UUID


class WorkspaceFromImageArtifactIn(Schema):
    """Request schema for creating a workspace from an image artifact."""

    name: str = ""
    credential_ids: list[uuid.UUID] = []


class WorkspaceFromImageArtifactOut(Schema):
    """Response schema after workspace creation from artifact is dispatched."""

    workspace_id: uuid.UUID
    task_id: uuid.UUID
    status: str


class ImageDefinitionBuildSummaryOut(Schema):
    """Per-runner build counts shown on collapsed image definition cards."""

    active: int = 0
    building: int = 0
    failed: int = 0
    inactive: int = 0
    removing: int = 0


class ImageBuildJobOut(Schema):
    """Response schema for runner-specific image build status."""

    id: uuid.UUID
    image_definition_id: uuid.UUID
    runner_id: uuid.UUID
    image_artifact_id: uuid.UUID | None = None
    status: str
    build_log: str
    build_task_id: uuid.UUID | None = None
    built_at: datetime | None = None
    deactivated_at: datetime | None = None
    delete_requested_at: datetime | None = None
    delete_confirmed_at: datetime | None = None
    delete_last_error: str = ""
    created_at: datetime
    updated_at: datetime


class ImageBuildJobCreateIn(Schema):
    """Assign runner + trigger build for an image definition."""

    runner_id: uuid.UUID
    activate: bool = True


class ImageBuildJobUpdateIn(Schema):
    """Update runner build lifecycle state via actions."""

    action: str  # deactivate | activate | rebuild


class ImageDefinitionOut(Schema):
    """Response schema for image definitions."""

    id: uuid.UUID
    organization_id: uuid.UUID | None = None
    created_by_id: int | None = None
    name: str
    description: str
    is_standard: bool = False
    runtime_type: str
    base_distro: str
    packages: list[str] = []
    env_vars: dict[str, str] = {}
    custom_dockerfile: str = ""
    custom_init_script: str = ""
    is_active: bool
    status: str = "active"
    runner_build_summary: ImageDefinitionBuildSummaryOut = (
        ImageDefinitionBuildSummaryOut()
    )
    delete_requested_at: datetime | None = None
    delete_started_at: datetime | None = None
    delete_confirmed_at: datetime | None = None
    delete_last_error: str = ""
    delete_attempt_count: int = 0
    created_at: datetime
    updated_at: datetime


class ImageDefinitionCreateIn(Schema):
    """Create schema for image definitions."""

    name: str
    description: str = ""
    runtime_type: str = "docker"
    base_distro: str = "ubuntu:22.04"
    packages: list[str] = []
    env_vars: dict[str, str] = {}
    custom_dockerfile: str = ""
    custom_init_script: str = ""
    is_active: bool = True


class ImageDefinitionUpdateIn(Schema):
    """Partial update schema for image definitions."""

    name: str | None = None
    description: str | None = None
    runtime_type: str | None = None
    base_distro: str | None = None
    packages: list[str] | None = None
    env_vars: dict[str, str] | None = None
    custom_dockerfile: str | None = None
    custom_init_script: str | None = None
    is_active: bool | None = None


class ImageDefinitionDuplicateIn(Schema):
    """Request schema for duplicating an image definition into the org."""

    name: str | None = None
