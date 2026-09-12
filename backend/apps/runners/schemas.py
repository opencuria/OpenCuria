"""
Pydantic schemas for the runners REST API.

Separated into input (In) and output (Out) schemas for clarity.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from ninja import Schema
from pydantic import ConfigDict, Field, field_validator, model_validator

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
    """Request schema for starting a background process.

    ``name`` is required and is the identity of the process within its
    workspace: a new name creates a fresh row, an existing name restarts
    the same row (upsert by name). Empty/blank names are rejected by the
    service with ``ValueError`` (mapped to 400) — no ``min_length``
    constraint here on purpose so empty names surface as 400, not 422.
    """

    command: str
    workdir: str = "/workspace"
    env: dict[str, str] = {}
    name: str


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
    run_count: int = 0
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


# ---------------------------------------------------------------------------
# Git schemas (productive git integration, whitelisted RPC)
# ---------------------------------------------------------------------------

#: Whitelisted git operations (mirrors GIT_OPERATIONS in runner/src/git.py).
GitOperation = Literal[
    "list_repos",
    "repo_snapshot",
    "repo_history",
    "working_diff",
    "commit_details",
    "stage",
    "unstage",
    "discard",
    "commit",
    "fetch",
    "pull",
    "push",
    "sync",
    "checkout_branch",
    "checkout_commit",
    "checkout_remote_branch",
    "create_branch",
    "rename_branch",
    "delete_branch",
    "merge_into_current",
    "merge_current_into",
    "merge_abort",
]

_HASH_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _validate_repo_path(value: str | None) -> str | None:
    """Basic backend check for absolute repo paths (runner re-validates)."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("repo_path must be a non-empty string")
    if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError("Invalid repo_path")
    normalized = os.path.normpath(cleaned)
    if normalized != "/workspace" and not normalized.startswith("/workspace/"):
        raise ValueError("repo_path must be under /workspace")
    if len(cleaned) > 512:
        raise ValueError("repo_path too long (max 512 chars)")
    return cleaned


def _validate_file_paths(value: list[str]) -> list[str]:
    """Validate repo-relative file paths (runner re-validates fail-closed)."""
    if not value:
        raise ValueError("paths must not be empty")
    if len(value) > 256:
        raise ValueError("Too many paths (max 256)")
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            raise ValueError("paths must not contain empty entries")
        if len(text) > 256:
            raise ValueError("path too long (max 256 chars)")
        if "\x00" in text or "\n" in text or "\r" in text:
            raise ValueError(f"Invalid path: {item!r}")
        cleaned.append(text)
    return cleaned


def _validate_branch(value: str, *, field: str = "branch") -> str:
    """Lightweight backend branch check (runner validates strictly)."""
    cleaned = str(value or "").strip()
    if not cleaned or len(cleaned) > 255:
        raise ValueError(f"Invalid {field}")
    if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError(f"Invalid {field}")
    return cleaned


def _validate_commit_hash(value: str) -> str:
    """Validate a hex commit hash (full or abbreviated, min 4 chars)."""
    cleaned = str(value or "").strip().lower()
    if not _HASH_RE.match(cleaned):
        raise ValueError("Invalid commit hash (must be 4-64 hex chars)")
    return cleaned


def validate_commit_hash(value: str) -> str:
    """Public commit-hash validator shared by REST schemas and views."""
    return _validate_commit_hash(value)


class GitRepoQuery(Schema):
    """Query params for GET /git/repo (per-repo snapshot)."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(..., max_length=512)

    @field_validator("repo_path")
    @classmethod
    def _check_repo_path(cls, value: str) -> str:
        result = _validate_repo_path(value)
        assert result is not None
        return result


class GitHistoryQuery(Schema):
    """Query params for GET /git/history (paged per-repo commit history)."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(..., max_length=512)
    history_limit: int = Field(default=50, ge=1, le=500)
    history_skip: int = Field(default=0, ge=0)
    branch: str | None = Field(default=None, max_length=255)

    @field_validator("repo_path")
    @classmethod
    def _check_repo_path(cls, value: str) -> str:
        result = _validate_repo_path(value)
        assert result is not None
        return result

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_branch(value)


class GitDiffQuery(Schema):
    """Query params for GET /git/diff (working-tree diff for one repo)."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(..., max_length=512)

    @field_validator("repo_path")
    @classmethod
    def _check_repo_path(cls, value: str) -> str:
        result = _validate_repo_path(value)
        assert result is not None
        return result


class GitCommitQuery(Schema):
    """Query params for GET /git/commits/{hash} (commit details)."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(..., max_length=512)

    @field_validator("repo_path")
    @classmethod
    def _check_repo_path(cls, value: str) -> str:
        result = _validate_repo_path(value)
        assert result is not None
        return result


class GitOperationIn(Schema):
    """Typed generic git operation request for POST /git/operation/.

    ``operation`` is a strict whitelist literal; all other fields are
    explicit and individually length-capped. Unknown keys are rejected
    (``extra='forbid'``). In particular there is no ``args`` dict and no
    ``env`` field — the service builds the runner payload field-by-field.
    Commit identity (``author_*``) is never accepted from the client; the
    API layer injects it server-side from ``request.user``. The webapp
    sends a plain ``message`` (no ``message_b64`` public API).
    """

    model_config = ConfigDict(extra="forbid")

    operation: GitOperation
    repo_path: str | None = Field(default=None, max_length=512)
    paths: list[str] | None = Field(default=None, max_length=256)
    message: str | None = Field(default=None, max_length=10000)
    branch: str | None = Field(default=None, max_length=255)
    commit: str | None = Field(default=None, max_length=64)
    target: str | None = Field(default=None, max_length=255)
    new_branch: str | None = Field(default=None, max_length=255)
    old_branch: str | None = Field(default=None, max_length=255)
    start_point: str | None = Field(default=None, max_length=255)
    remote: str | None = Field(default=None, max_length=255)
    remote_ref: str | None = Field(default=None, max_length=255)
    local_name: str | None = Field(default=None, max_length=255)
    checkout: bool | None = None
    set_upstream: bool | None = None
    history_limit: int | None = Field(default=None, ge=1, le=500)
    history_skip: int | None = Field(default=None, ge=0)

    @field_validator("repo_path")
    @classmethod
    def _check_repo_path(cls, value: str | None) -> str | None:
        return _validate_repo_path(value)

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_file_paths(value)

    @field_validator("branch", "target", "new_branch", "old_branch", "start_point", "local_name")
    @classmethod
    def _check_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_branch(value)

    @field_validator("remote_ref")
    @classmethod
    def _check_remote_ref(cls, value: str | None) -> str | None:
        """Lightweight backend check for ``remote/branch`` refs (runner validates strictly)."""
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned or len(cleaned) > 255:
            raise ValueError("Invalid remote_ref")
        if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
            raise ValueError("Invalid remote_ref")
        remote, sep, branch_part = cleaned.partition("/")
        if not sep or not remote or not branch_part:
            raise ValueError("Invalid remote_ref")
        return cleaned

    @field_validator("commit")
    @classmethod
    def _check_commit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_commit_hash(value)

    @field_validator("message")
    @classmethod
    def _check_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 10000:
            raise ValueError("message too long (max 10000 chars)")
        return value

    @field_validator("remote")
    @classmethod
    def _check_remote(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 255:
            raise ValueError("Invalid remote")
        if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
            raise ValueError("Invalid remote")
        return cleaned

    @model_validator(mode="after")
    def _check_operation_fields(self) -> "GitOperationIn":
        """Require the fields each operation needs (fail fast, 422)."""
        op = self.operation
        needs_repo = op not in {"list_repos"}
        if needs_repo and not self.repo_path:
            raise ValueError(f"repo_path is required for operation {op!r}")
        if op in {"stage", "unstage", "discard"} and not self.paths:
            raise ValueError(f"paths is required for operation {op!r}")
        if op == "commit" and not (self.message or "").strip():
            raise ValueError("message is required for commit")
        if op == "commit_details" and not (self.commit or "").strip():
            raise ValueError("commit is required for commit_details")
        if op == "checkout_branch" and not (self.branch or "").strip():
            raise ValueError("branch is required for checkout_branch")
        if op == "checkout_commit" and not (self.commit or "").strip():
            raise ValueError("commit is required for checkout_commit")
        if op == "checkout_remote_branch" and not (self.remote_ref or "").strip():
            raise ValueError("remote_ref is required for checkout_remote_branch")
        if op == "create_branch" and not (self.branch or "").strip():
            raise ValueError("branch is required for create_branch")
        if op == "rename_branch" and not (self.new_branch or "").strip():
            raise ValueError("new_branch is required for rename_branch")
        if op == "delete_branch" and not (self.branch or "").strip():
            raise ValueError("branch is required for delete_branch")
        if op == "merge_into_current" and not (self.branch or "").strip():
            raise ValueError("branch is required for merge_into_current")
        if op == "merge_current_into" and not (self.target or "").strip():
            raise ValueError("target is required for merge_current_into")
        if op == "pull" and (self.branch or "").strip() and not (self.remote or "").strip():
            raise ValueError("remote is required when branch is set for pull")
        if (
            op in {"merge_into_current", "merge_current_into"}
            and self.message is not None
            and len(self.message) > 4096
        ):
            raise ValueError("message too long (max 4096 chars)")
        return self
