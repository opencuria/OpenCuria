"""
Enums for the runners app.

Mirror the state machines from the runner codebase so the backend
can enforce identical transitions.
"""

from __future__ import annotations

from django.db import models


class RuntimeType(models.TextChoices):
    """Virtualisation backend for workspaces."""

    DOCKER = "docker", "Docker"
    QEMU = "qemu", "QEMU/KVM"


class RunnerStatus(models.TextChoices):
    """Connection status of a runner."""

    ONLINE = "online", "Online"
    OFFLINE = "offline", "Offline"


class WorkspaceStatus(models.TextChoices):
    """Lifecycle status of a workspace — mirrors runner WorkspaceStatus."""

    CREATING = "creating", "Creating"
    RUNNING = "running", "Running"
    STOPPED = "stopped", "Stopped"
    FAILED = "failed", "Failed"
    REMOVED = "removed", "Removed"


class WorkspaceOperation(models.TextChoices):
    """Current blocking lifecycle operation running against a workspace."""

    CREATING = "creating", "Creating"
    STARTING = "starting", "Starting"
    STOPPING = "stopping", "Stopping"
    RESTARTING = "restarting", "Restarting"
    REMOVING = "removing", "Removing"
    CAPTURING_IMAGE = "capturing_image", "Capturing Image"


class SessionStatus(models.TextChoices):
    """Status of a prompt session — mirrors runner SessionStatus."""

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class TaskType(models.TextChoices):
    """Types of tasks the backend can dispatch to runners."""

    CREATE_WORKSPACE = "create_workspace", "Create Workspace"
    UPDATE_WORKSPACE = "update_workspace", "Update Workspace"
    RUN_PROMPT = "run_prompt", "Run Prompt"
    CANCEL_SESSION = "cancel_session", "Cancel Session"
    STOP_WORKSPACE = "stop_workspace", "Stop Workspace"
    RESUME_WORKSPACE = "resume_workspace", "Resume Workspace"
    REMOVE_WORKSPACE = "remove_workspace", "Remove Workspace"
    START_TERMINAL = "start_terminal", "Start Terminal"
    START_DESKTOP = "start_desktop", "Start Desktop"
    STOP_DESKTOP = "stop_desktop", "Stop Desktop"
    CREATE_IMAGE_ARTIFACT = "create_image_artifact", "Create Image Artifact"
    CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT = (
        "create_workspace_from_image_artifact",
        "Create Workspace From Image Artifact",
    )
    BUILD_IMAGE = "build_image", "Build Image"


class TaskStatus(models.TextChoices):
    """Execution status of a dispatched task."""

    PENDING = "pending", "Pending"
    IN_PROGRESS = "in_progress", "In Progress"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class AgentCommandPhase(models.TextChoices):
    """Phase in which an agent command is executed."""

    CONFIGURE = "configure", "Configure"
    RUN = "run", "Run"
    RUN_FIRST = "run_first", "Run (First Message)"
