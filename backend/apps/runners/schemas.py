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


class ImageVersionRefOut(Schema):
    """The image version a workspace is (or is being) based on."""

    id: uuid.UUID
    line_kind: str | None = None
    line_id: uuid.UUID | None = None
    name: str
    version: int | None = None
    message: str = ""
    status: str
    latest_id: uuid.UUID | None = None
    latest_version: int | None = None
    update_available: bool = False


class WorkspaceDiskUsageOut(Schema):
    """Last observed filesystem usage inside a workspace."""

    used_bytes: int
    free_bytes: int
    total_bytes: int
    observed_at: datetime


class WorkspaceOut(Schema):
    """Response schema for a workspace."""

    id: uuid.UUID
    runner_id: uuid.UUID
    status: str
    active_operation: str | None = None
    intervention_required: bool = False
    lifecycle_diagnostic: str = ""
    name: str
    qemu_vcpus: int | None = None
    qemu_memory_mb: int | None = None
    qemu_disk_size_gb: int | None = None
    disk_usage: WorkspaceDiskUsageOut | None = None
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
    plugin_ids: list[uuid.UUID] = []
    credentials_present: bool = False
    base_image_name: str | None = None
    base_image: ImageVersionRefOut | None = None
    pending_base_image: ImageVersionRefOut | None = None
    repos: list[str] = []


class WorkspaceRecreateIn(Schema):
    """Recreate a workspace on its own version (reset) or the latest (update)."""

    image_id: uuid.UUID


class WorkspaceRecreateOut(Schema):
    """Response after a recreate was accepted."""

    workspace_id: uuid.UUID
    task_id: uuid.UUID
    active_operation: str | None = None


class WorkspaceCreateIn(Schema):
    """Request schema for creating a workspace."""

    name: str
    repos: list[str] = []
    credential_ids: list[uuid.UUID] = []
    plugin_ids: list[uuid.UUID] = []
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
    plugin_ids: list[uuid.UUID] | None = None
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
    intervention_required: bool = False
    lifecycle_diagnostic: str = ""
    credential_ids: list[uuid.UUID] = []
    plugin_ids: list[uuid.UUID] = []
    credentials_present: bool = False
    credential_sync_status: str = "not_required"
    credential_sync_detail: str | None = None
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
    plugin_ids: list[uuid.UUID] = []


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


class DesktopViewerIntentIn(Schema):
    """A tab-local viewer intent, scoped to the authenticated user."""

    viewer_client_id: uuid.UUID
    intent_revision: int = Field(gt=0, strict=True)


class DesktopRenewOut(Schema):
    """Own renewable viewer intent only."""

    viewer_lease_state: str
    revision: int | None = None
    epoch: str | None = None


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
    mcp_active: bool = False
    holder_count: int = 0
    viewer_lease_state: str = "unknown"
    revision: int | None = None
    epoch: str | None = None


class DesktopTakeControlOut(Schema):
    """Response schema after taking control of the desktop from computer-use."""

    aborted_session_ids: list[uuid.UUID]


class DesktopClipboardWriteIn(Schema):
    """Request schema for writing plain text into the VM clipboard."""

    text: str


class DesktopClipboardReadIn(Schema):
    """Optional request options for reading or copying desktop text."""

    model_config = ConfigDict(extra="forbid")

    copy_selection: bool = Field(default=False, strict=True)
    shift: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _shift_requires_selection(self) -> DesktopClipboardReadIn:
        if self.shift and not self.copy_selection:
            raise ValueError("shift requires copy_selection")
        return self


class DesktopClipboardReadOut(Schema):
    """Response schema for reading plain text from the VM clipboard."""

    text: str


# ---------------------------------------------------------------------------
# Background process schemas
# ---------------------------------------------------------------------------


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
    kind: str = "persistent"
    started_at: datetime
    ended_at: datetime | None = None
    updated_at: datetime


# ---------------------------------------------------------------------------
# Image schemas
# ---------------------------------------------------------------------------


class ImageVersionWorkspaceOut(Schema):
    """A workspace that is based on an image version."""

    id: uuid.UUID
    name: str
    created_by_id: int | None = None


class ImageArtifactOut(Schema):
    """Response schema for a concrete image artifact (one image version)."""

    id: uuid.UUID
    source_workspace_id: uuid.UUID | None = None
    runner_artifact_id: str
    name: str
    size_bytes: int | None
    status: str
    artifact_kind: str = "captured"
    revision_id: uuid.UUID | None = None
    generation: int | None = None
    is_legacy: bool = False
    is_current: bool = False
    build_job_id: uuid.UUID | None = None
    source_definition_name: str | None = None
    source_runner_id: uuid.UUID | None = None
    is_deactivated: bool = False
    source_runner_online: bool = False
    delete_requested_at: datetime | None = None
    delete_confirmed_at: datetime | None = None
    delete_last_error: str = ""
    created_at: datetime
    created_by_id: int | None = None
    captured_image_id: uuid.UUID | None = None
    version: int | None = None
    message: str = ""
    is_latest: bool = False
    retention: str | None = None
    workspace_count: int = 0
    workspaces: list[ImageVersionWorkspaceOut] = []


class ImageArtifactCreateIn(Schema):
    """Capture a workspace as a new image or as the next version of one."""

    name: str = ""
    captured_image_id: uuid.UUID | None = None
    message: str = Field(default="", max_length=500)


class CapturedImageOut(Schema):
    """A versioned captured image (line) with all of its versions."""

    id: uuid.UUID
    name: str
    status: str
    runner_id: uuid.UUID
    runner_online: bool = False
    created_by_id: int | None = None
    created_at: datetime
    latest_id: uuid.UUID | None = None
    latest_version: int | None = None
    total_size_bytes: int = 0
    workspace_count: int = 0
    versions: list[ImageArtifactOut] = []


class CapturedImageUpdateIn(Schema):
    """Rename a captured image."""

    name: str


class ImageArtifactCreateOut(Schema):
    """Response schema after artifact creation is dispatched."""

    task_id: uuid.UUID
    workspace_id: uuid.UUID


class WorkspaceFromImageArtifactIn(Schema):
    """Request schema for creating a workspace from an image artifact."""

    name: str = ""
    credential_ids: list[uuid.UUID] = []
    plugin_ids: list[uuid.UUID] = []


class WorkspaceFromImageArtifactOut(Schema):
    """Response schema after workspace creation from artifact is dispatched."""

    workspace_id: uuid.UUID
    task_id: uuid.UUID
    status: str
    plugin_ids: list[uuid.UUID] = []


class ImageDefinitionBuildSummaryOut(Schema):
    """Per-runner build counts shown on collapsed image definition cards."""

    active: int = 0
    building: int = 0
    failed: int = 0
    inactive: int = 0
    removing: int = 0


class ImageBuildJobOut(Schema):
    """Response schema for runner-specific image build status.

    Includes the full ``build_log`` — used only for single-object create /
    update responses. List responses use :class:`ImageBuildJobListOut`,
    which carries ``build_log_size`` instead and keeps the 3s frontend
    polling cheap.
    """

    id: uuid.UUID
    image_definition_id: uuid.UUID
    runner_id: uuid.UUID
    image_artifact_id: uuid.UUID | None = None
    current_generation_id: uuid.UUID | None = None
    pending_generation_id: uuid.UUID | None = None
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


class ImageBuildJobListOut(Schema):
    """Lightweight list schema for runner image builds (polling-safe).

    Same fields as :class:`ImageBuildJobOut` except the full ``build_log``
    is replaced by ``build_log_size`` (characters). The log itself is
    fetched on demand via ``GET .../runner-builds/{runner_id}/log/``.
    """

    id: uuid.UUID
    image_definition_id: uuid.UUID
    runner_id: uuid.UUID
    image_artifact_id: uuid.UUID | None = None
    current_generation_id: uuid.UUID | None = None
    pending_generation_id: uuid.UUID | None = None
    status: str
    build_log_size: int = 0
    build_task_id: uuid.UUID | None = None
    built_at: datetime | None = None
    deactivated_at: datetime | None = None
    delete_requested_at: datetime | None = None
    delete_confirmed_at: datetime | None = None
    delete_last_error: str = ""
    created_at: datetime
    updated_at: datetime
    versions: list[ImageArtifactOut] = []


class ImageBuildJobCreateIn(Schema):
    """Assign runner + trigger build for an image definition."""

    runner_id: uuid.UUID
    activate: bool = True
    message: str = Field(default="", max_length=500)


class ImageBuildJobUpdateIn(Schema):
    """Update runner build lifecycle state via actions."""

    action: str  # deactivate | activate | rebuild
    message: str = Field(default="", max_length=500)


class ImageDefinitionOut(Schema):
    """Response schema for image definitions."""

    id: uuid.UUID
    organization_id: uuid.UUID | None = None
    created_by_id: int | None = None
    name: str
    description: str
    is_standard: bool = False
    base_distro: str
    packages: list[str] = []
    env_vars: dict[str, str] = {}
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
    base_distro: str = "ubuntu:22.04"
    packages: list[str] = []
    env_vars: dict[str, str] = {}
    custom_init_script: str = ""
    is_active: bool = True


class ImageDefinitionUpdateIn(Schema):
    """Partial update schema for image definitions."""

    name: str | None = None
    description: str | None = None
    base_distro: str | None = None
    packages: list[str] | None = None
    env_vars: dict[str, str] | None = None
    custom_init_script: str | None = None
    is_active: bool | None = None


class ImageDefinitionDuplicateIn(Schema):
    """Request schema for duplicating an image definition into the org."""

    name: str | None = None


# ---------------------------------------------------------------------------
# Git schemas (productive git integration, whitelisted RPC)
# ---------------------------------------------------------------------------

#: Git mutations accepted by POST /git/operation/; reads have GET endpoints.
GitOperation = Literal[
    "stage_patch",
    "unstage_patch",
    "discard_patch",
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
    "stash_apply",
    "stash_pop",
    "stash_drop",
    "stash_branch",
]

_HASH_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _validate_repo_path(value: str) -> str:
    """Basic backend check for absolute repo paths (runner re-validates)."""
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
        text = str(item)
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


_STASH_SELECTOR_RE = re.compile(r"^stash@\{\d{1,9}\}$")


def _validate_stash_selector(value: str, *, field: str = "stash") -> str:
    """Validate a stash selector (``stash@{n}``; runner re-validates strictly)."""
    cleaned = str(value or "").strip()
    if not _STASH_SELECTOR_RE.match(cleaned):
        raise ValueError(f"Invalid {field} (must be stash@{{n}})")
    return cleaned


def validate_commit_hash(value: str) -> str:
    """Public commit-hash validator shared by REST schemas and views."""
    return _validate_commit_hash(value)


class GitRepoQuery(Schema):
    """Validated repository path shared by Git reads and mutations."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(..., max_length=512)

    @field_validator("repo_path")
    @classmethod
    def _check_repo_path(cls, value: str) -> str:
        return _validate_repo_path(value)


class EditorFileWriteIn(Schema):
    """Complete text and the revision the editor originally read."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1, max_length=4096)
    content: str = Field(..., max_length=5 * 1024 * 1024)
    expected_revision: str = Field(..., min_length=7, max_length=64)


class GitHistoryQuery(GitRepoQuery):
    """Query params for GET /git/history (paged per-repo commit history)."""

    history_limit: int = Field(default=50, ge=1, le=500)
    history_skip: int = Field(default=0, ge=0)
    branch: str | None = Field(default=None, max_length=255)

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_branch(value)


class GitOperationIn(GitRepoQuery):
    """Typed Git mutation request for POST /git/operation/.

    ``operation`` is a strict whitelist literal; all other fields are
    explicit and individually length-capped. Unknown keys are rejected
    (``extra='forbid'``). In particular there is no ``args`` dict and no
    ``env`` field. Commit identity (``author_*``) is never accepted from
    the client; the service injects it from ``request.user``. The webapp
    sends a plain ``message`` (no ``message_b64`` public API).
    """

    operation: GitOperation
    path: str | None = Field(default=None, max_length=1024)
    patch: str | None = Field(default=None, max_length=200 * 1024)
    expected_index_revision: str | None = Field(default=None, max_length=64)
    expected_worktree_revision: str | None = Field(default=None, max_length=64)
    expected_head_revision: str | None = Field(default=None, max_length=64)
    old_path: str | None = Field(default=None, max_length=1024)
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
    stash: str | None = Field(default=None, max_length=32)

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_file_paths(value)

    @field_validator(
        "branch", "target", "new_branch", "old_branch", "start_point", "local_name"
    )
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

    @field_validator("stash")
    @classmethod
    def _check_stash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_stash_selector(value)

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
    def _check_operation_fields(self) -> GitOperationIn:
        """Require the fields each operation needs (fail fast, 422)."""
        op = self.operation
        if op in {"stage_patch", "unstage_patch", "discard_patch"}:
            if not self.path or not self.patch:
                raise ValueError("path and patch are required")
            if not self.expected_index_revision or not self.expected_worktree_revision:
                raise ValueError("Expected file and index revisions are required")
            if op == "unstage_patch" and not self.expected_head_revision:
                raise ValueError("Expected HEAD revision is required")
        if op in {"stage", "unstage", "discard"} and not self.paths:
            raise ValueError(f"paths is required for operation {op!r}")
        if op == "commit" and not (self.message or "").strip():
            raise ValueError("message is required for commit")
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
        if (
            op == "pull"
            and (self.branch or "").strip()
            and not (self.remote or "").strip()
        ):
            raise ValueError("remote is required when branch is set for pull")
        if (
            op in {"stash_apply", "stash_pop", "stash_drop"}
            and not (self.stash or "").strip()
        ):
            raise ValueError(f"stash is required for {op}")
        if op == "stash_branch":
            if not (self.stash or "").strip():
                raise ValueError("stash is required for stash_branch")
            if not (self.branch or "").strip():
                raise ValueError("branch is required for stash_branch")
        if (
            op in {"merge_into_current", "merge_current_into"}
            and self.message is not None
            and len(self.message) > 4096
        ):
            raise ValueError("message too long (max 4096 chars)")
        return self
