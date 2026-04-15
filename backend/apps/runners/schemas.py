"""
Pydantic schemas for the runners REST API.

Separated into input (In) and output (Out) schemas for clarity.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ninja import Schema

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
    created_by_id: int
    last_activity_at: datetime
    auto_stop_timeout_minutes: int | None = None
    auto_stop_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    has_active_session: bool = False
    runner_online: bool = False
    credential_ids: list[uuid.UUID] = []


class WorkspaceDetailOut(WorkspaceOut):
    """Response schema for a workspace with its sessions."""

    sessions: list[SessionOut] = []


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
    image_artifact_id: uuid.UUID


class WorkspaceUpdateIn(Schema):
    """Request schema for updating workspace metadata."""

    name: str | None = None
    credential_ids: list[uuid.UUID] | None = None
    qemu_vcpus: int | None = None
    qemu_memory_mb: int | None = None
    qemu_disk_size_gb: int | None = None


class WorkspaceUpdateOut(Schema):
    """Response schema for workspace metadata updates."""

    id: uuid.UUID
    name: str
    updated_at: datetime
    active_operation: str | None = None
    credential_ids: list[uuid.UUID] = []
    qemu_vcpus: int | None = None
    qemu_memory_mb: int | None = None
    qemu_disk_size_gb: int | None = None


class WorkspaceCreateOut(Schema):
    """Response schema after workspace creation is dispatched."""

    workspace_id: uuid.UUID
    task_id: uuid.UUID
    status: str


# ---------------------------------------------------------------------------
# Session schemas
# ---------------------------------------------------------------------------


class SessionSkillOut(Schema):
    """Snapshot of a skill attached to a session."""

    id: uuid.UUID
    skill_id: uuid.UUID | None
    name: str
    body: str
    created_at: datetime


class SessionOut(Schema):
    """Response schema for a session."""

    id: uuid.UUID
    chat_id: uuid.UUID
    prompt: str
    agent_model: str = ""
    agent_options: dict = {}
    output: str
    error_message: str | None = None
    status: str
    read_at: datetime | None = None
    created_at: datetime
    completed_at: datetime | None
    skills: list[SessionSkillOut] = []


# ---------------------------------------------------------------------------
# Prompt schemas
# ---------------------------------------------------------------------------


class PromptIn(Schema):
    """Request schema for running a prompt."""

    prompt: str
    agent_model: str | None = None
    agent_options: dict[str, str] = {}
    chat_id: str | None = None
    skill_ids: list[uuid.UUID] = []


class PromptOut(Schema):
    """Response schema after prompt is dispatched."""

    session_id: uuid.UUID
    task_id: uuid.UUID
    chat_id: uuid.UUID
    status: str


# ---------------------------------------------------------------------------
# Task schemas
# ---------------------------------------------------------------------------


class TaskOut(Schema):
    """Response schema for a task."""

    id: uuid.UUID
    runner_id: uuid.UUID
    workspace_id: uuid.UUID | None
    session_id: uuid.UUID | None
    type: str
    status: str
    error: str
    created_at: datetime
    completed_at: datetime | None


# ---------------------------------------------------------------------------
# Agent schemas
# ---------------------------------------------------------------------------


class AgentCommandOut(Schema):
    """Response schema for an agent command."""

    id: uuid.UUID
    phase: str
    args: list[str]
    workdir: str | None = None
    env: dict[str, str] = {}
    description: str = ""
    order: int = 0


class AgentCommandIn(Schema):
    """Request schema for creating/updating an agent command."""

    phase: str
    args: list[str]
    workdir: str | None = None
    env: dict[str, str] = {}
    description: str = ""
    order: int = 0


class AgentOptionOut(Schema):
    """Response schema for a single selectable agent option."""

    key: str
    label: str
    choices: list[str]
    default: str = ""


class AgentOut(Schema):
    """Response schema for an available agent type."""

    id: uuid.UUID
    name: str
    description: str = ""
    available_options: list[AgentOptionOut] = []
    default_env: dict[str, Any] = {}
    supports_multi_chat: bool = False
    has_online_runner: bool = False
    required_credential_service_slugs: list[str] = []
    has_credentials: bool = False


class AgentDetailOut(Schema):
    """Response schema for a detailed agent definition."""

    id: uuid.UUID
    name: str
    description: str = ""
    configure_commands: list[AgentCommandOut] = []
    run_command: AgentCommandOut | None = None


# ---------------------------------------------------------------------------
# Org agent definition management schemas
# ---------------------------------------------------------------------------


class OrgAgentDefinitionOut(Schema):
    """Full agent definition output for org admin management."""

    id: uuid.UUID
    name: str
    description: str = ""
    is_standard: bool = True
    organization_id: uuid.UUID | None = None
    available_options: list[AgentOptionOut] = []
    default_env: dict[str, Any] = {}
    supports_multi_chat: bool = False
    required_credential_service_ids: list[uuid.UUID] = []
    commands: list[AgentCommandOut] = []
    is_active: bool = False


class OrgAgentDefinitionCreateIn(Schema):
    """Request schema for creating an org-specific agent definition."""

    name: str
    description: str = ""
    available_options: list[dict] = []
    default_env: dict[str, Any] = {}
    supports_multi_chat: bool = False
    required_credential_service_ids: list[uuid.UUID] = []
    commands: list[AgentCommandIn] = []


class OrgAgentDefinitionUpdateIn(Schema):
    """Request schema for updating an org-specific agent definition."""

    name: str | None = None
    description: str | None = None
    available_options: list[dict] | None = None
    default_env: dict[str, Any] | None = None
    supports_multi_chat: bool | None = None
    required_credential_service_ids: list[uuid.UUID] | None = None
    commands: list[AgentCommandIn] | None = None


class OrgAgentDefinitionDuplicateIn(Schema):
    """Request schema for duplicating an agent definition into the org."""

    name: str | None = None
    activate: bool = True


class OrgAgentActivationToggleIn(Schema):
    """Request schema for activating/deactivating an agent definition for an org."""

    active: bool


# ---------------------------------------------------------------------------
# Agent credential relation schemas
# ---------------------------------------------------------------------------


class AgentCredentialRelationCommandOut(Schema):
    """Response schema for a credential-relation command."""

    id: uuid.UUID
    phase: str
    args: list[str]
    workdir: str | None = None
    env: dict[str, str] = {}
    description: str = ""
    order: int = 0


class AgentCredentialRelationCommandIn(Schema):
    """Request schema for creating/updating a credential-relation command."""

    phase: str
    args: list[str]
    workdir: str | None = None
    env: dict[str, str] = {}
    description: str = ""
    order: int = 0


class AgentCredentialRelationOut(Schema):
    """Response schema for an agent-credential relation."""

    id: uuid.UUID
    credential_service_id: uuid.UUID
    credential_service_name: str = ""
    default_env: dict[str, Any] = {}
    commands: list[AgentCredentialRelationCommandOut] = []


class AgentCredentialRelationCreateIn(Schema):
    """Request schema for creating an agent-credential relation."""

    credential_service_id: uuid.UUID
    default_env: dict[str, Any] = {}
    commands: list[AgentCredentialRelationCommandIn] = []


class AgentCredentialRelationUpdateIn(Schema):
    """Request schema for updating an agent-credential relation."""

    default_env: dict[str, Any] | None = None
    commands: list[AgentCredentialRelationCommandIn] | None = None


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


class DesktopClipboardWriteIn(Schema):
    """Request schema for writing plain text into the VM clipboard."""

    text: str


class DesktopClipboardReadOut(Schema):
    """Response schema for reading plain text from the VM clipboard."""

    text: str


# ---------------------------------------------------------------------------
# Error schemas
# ---------------------------------------------------------------------------


class ErrorOut(Schema):
    """Standard error response."""

    detail: str
    code: str = "error"


# ---------------------------------------------------------------------------
# Chat schemas
# ---------------------------------------------------------------------------


class ChatOut(Schema):
    """Response schema for a chat."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    agent_definition_id: uuid.UUID | None = None
    agent_type: str = ""
    created_at: datetime
    updated_at: datetime
    session_count: int = 0


class ChatCreateIn(Schema):
    """Request schema for creating a chat."""

    name: str = ""
    agent_definition_id: uuid.UUID | None = None


class ChatRenameIn(Schema):
    """Request schema for renaming a chat."""

    name: str


# ---------------------------------------------------------------------------
# Conversation schemas
# ---------------------------------------------------------------------------


class LastSessionOut(Schema):
    """Response schema for the most recent session in a conversation."""

    id: uuid.UUID
    prompt: str
    status: str  # pending / running / completed / failed
    created_at: datetime


class ConversationOut(Schema):
    """Response schema for a conversation entry (chat or workspace)."""

    chat_id: uuid.UUID | None
    workspace_id: uuid.UUID
    workspace_name: str
    workspace_status: str
    agent_definition_id: uuid.UUID | None = None
    agent_type: str
    chat_name: str  # empty string for single-chat workspace fallbacks
    last_session: LastSessionOut | None
    session_count: int
    updated_at: datetime
    is_read: bool = True


class MarkConversationReadIn(Schema):
    """Request schema for marking a conversation as read."""

    session_id: uuid.UUID


class MarkConversationUnreadIn(Schema):
    """Request schema for marking a conversation as unread."""

    session_id: uuid.UUID


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
    runner_image_build_id: uuid.UUID | None = None
    source_definition_name: str | None = None
    source_runner_id: uuid.UUID | None = None
    runtime_type: str | None = None
    is_deactivated: bool = False
    source_runner_online: bool = False
    created_at: datetime
    created_by_id: int | None = None
    credential_ids: list[uuid.UUID] = []


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
    """Request schema for creating a workspace from an image artifact.

    Credentials are automatically restored from the artifact — no need to
    specify them. The runner, runtime and resources come from the artifact's
    source workspace.
    """

    name: str = ""


class WorkspaceFromImageArtifactOut(Schema):
    """Response schema after workspace creation from artifact is dispatched."""

    workspace_id: uuid.UUID
    task_id: uuid.UUID
    status: str


class RunnerImageBuildOut(Schema):
    """Response schema for runner-specific image build status."""

    id: uuid.UUID
    image_definition_id: uuid.UUID
    runner_id: uuid.UUID
    image_artifact_id: uuid.UUID | None = None
    status: str
    image_tag: str
    image_path: str
    build_log: str
    build_task_id: uuid.UUID | None = None
    built_at: datetime | None = None
    deactivated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RunnerImageBuildCreateIn(Schema):
    """Assign runner + trigger build for an image definition."""

    runner_id: uuid.UUID
    activate: bool = True


class RunnerImageBuildUpdateIn(Schema):
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


# Fix forward reference in WorkspaceDetailOut
WorkspaceDetailOut.model_rebuild()
