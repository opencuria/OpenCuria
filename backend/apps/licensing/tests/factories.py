"""Builders for licensing tests: users, images and workspaces in real flows."""

from __future__ import annotations

import uuid
from typing import Any

from django.contrib.auth import get_user_model

from apps.runners.enums import TaskType
from apps.runners.guest_os import WINDOWS
from apps.runners.models import CapturedImage, ImageInstance, Runner, Workspace
from apps.runners.services.workspace_capacity import WorkspaceCapacityService
from apps.runners.services.workspace_configuration import WorkspaceConfigurationService


def product_key(number: int) -> str:
    """A syntactically valid, unique product key."""
    return f"K{number:04d}-AAAAA-BBBBB-CCCCC-DDDDD"


def make_user(email_prefix: str, *, first_name: str = "") -> Any:
    """Create a user with a unique email."""
    return get_user_model().objects.create_user(
        email=f"{email_prefix}-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
        first_name=first_name,
    )


def image(runner: Runner, owner: Any, guest_os: str = WINDOWS) -> ImageInstance:
    """A ready captured image (v1 of a new line) owned by ``owner``."""
    line = CapturedImage.objects.create(
        organization=runner.organization,
        runner=runner,
        created_by=owner,
        name=f"{guest_os} image",
    )
    return ImageInstance.objects.create(
        runner=runner,
        origin_type="workspace_capture",
        captured_image=line,
        generation=1,
        name=f"{guest_os} image",
        created_by=owner,
        status="ready",
        runner_ref=f"/images/{uuid.uuid4()}.qcow2",
        guest_os=guest_os,
    )


def create(
    runner: Runner,
    owner: Any,
    guest_os: str = WINDOWS,
    *,
    task_type: TaskType = TaskType.CREATE_WORKSPACE,
) -> Workspace:
    """Create (or clone) a workspace through the transactional create service."""
    workspace_id = uuid.uuid4()
    workspace, _ = WorkspaceConfigurationService().create(
        workspace_fields={
            "workspace_id": workspace_id,
            "runner": runner,
            "name": "Licensed workspace",
            "created_by": owner,
            "base_image_instance": image(runner, owner, guest_os),
            "qemu_disk_size_gb": 64,
        },
        runner=runner,
        user=owner,
        organization_id=runner.organization_id,
        credentials=[],
        plugin_ids=[],
        task_id=uuid.uuid4(),
        operation_payload={"workspace_id": str(workspace_id)},
        task_type=task_type,
    )
    return workspace


def settle(workspace: Workspace, status: str) -> Workspace:
    """Finish the workspace's operation in ``status`` (as a runner result would)."""
    Workspace.objects.filter(pk=workspace.pk).update(
        status=status, current_task=None, active_operation=None
    )
    workspace.refresh_from_db()
    return workspace


def start(workspace: Workspace) -> Workspace:
    """Start a stopped workspace through the transactional resume service."""
    started, _ = WorkspaceCapacityService.resume(workspace.id, uuid.uuid4())
    started.refresh_from_db()
    return started
