"""Characterize the atomic workspace configuration workflow."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.models import (
    Credential,
    CredentialService,
    OrgCredentialServiceActivation,
)
from apps.credentials.services import CredentialSvc
from apps.plugins.models import Plugin, WorkspacePluginActivation
from apps.plugins.services import PluginService
from apps.runners.enums import TaskType
from apps.runners.models import (
    CapturedImage,
    ImageInstance,
    LifecycleCommand,
    Runner,
    Task,
    Workspace,
    WorkspaceProcess,
)
from apps.runners.repositories import TaskRepository, WorkspaceRepository
from apps.runners.services.workspace_configuration import WorkspaceConfigurationService
from common.exceptions import ConflictError, NotFoundError

pytestmark = pytest.mark.django_db(transaction=True)
Selection = tuple[ImageInstance, Credential, Plugin]


@pytest.fixture
def selection(runner: Runner, user: Any) -> Selection:
    """A selectable image, credential and enabled plugin with no requirements."""
    catalog = CredentialService.objects.create(
        name="Workspace token",
        slug="configuration-token",
        credential_type="env",
        env_var_name="WORKSPACE_TOKEN",
    )
    OrgCredentialServiceActivation.objects.create(
        organization=runner.organization, credential_service=catalog
    )
    credential = CredentialSvc().create_personal_credential(
        service_id=catalog.id,
        name="Token",
        value="workspace-secret",
        user=user,
        org_id=runner.organization_id,
    )
    plugin = Plugin.objects.create(name="Tools", slug="configuration-tools")
    PluginService().set_org_activation(
        plugin.id, org_id=runner.organization_id, user=user, active=True
    )
    line = CapturedImage.objects.create(
        organization=runner.organization, runner=runner, created_by=user, name="Image"
    )
    image = ImageInstance.objects.create(
        runner=runner,
        captured_image=line,
        generation=1,
        created_by=user,
        runner_ref="/images/configuration.qcow2",
        status="ready",
        process_definitions=[
            {"name": "web", "command": "python -m app", "workdir": "/app"}
        ],
    )
    return image, credential, plugin


def _create(
    service: WorkspaceConfigurationService,
    runner: Runner,
    user: Any,
    selection: Selection,
    workspace_id: uuid.UUID,
) -> tuple[Workspace, Task]:
    """Create a fully configured workspace through the public service."""
    image, credential, plugin = selection
    return service.create(
        workspace_fields={
            "workspace_id": workspace_id,
            "runner": runner,
            "name": "Configured",
            "created_by": user,
            "base_image_instance": image,
        },
        runner=runner,
        user=user,
        organization_id=runner.organization_id,
        credentials=[credential],
        plugin_ids=[plugin.id, plugin.id],
        task_id=uuid.uuid4(),
        operation_payload={"workspace_id": str(workspace_id)},
        credentials_present=True,
    )


def test_create_commits_selections_processes_and_lifecycle_task(
    runner: Runner, user: Any, selection: Selection
) -> None:
    """Creation returns fully loaded state and one durable command for the task."""
    workspace, task = _create(
        WorkspaceConfigurationService(), runner, user, selection, uuid.uuid4()
    )
    image, credential, plugin = selection
    assert workspace.base_image_instance_id == image.id
    assert workspace.credentials_present is True
    assert list(workspace.credentials.all()) == [credential]
    assert list(workspace.plugin_activations.values_list("plugin_id", flat=True)) == [
        plugin.id
    ]
    process = WorkspaceProcess.objects.get(workspace=workspace)
    assert (process.name, process.command, process.workdir) == (
        "web",
        "python -m app",
        "/app",
    )
    assert process.kind == "persistent" and process.run_count == 0
    assert process.created_by_id == user.id
    assert task.workspace_id == workspace.id and task.type == TaskType.CREATE_WORKSPACE
    assert LifecycleCommand.objects.get(task=task).event == "task:create_workspace"


@pytest.mark.parametrize("operation", ["create", "update"])
def test_late_task_failure_rolls_back_all_configuration(
    runner: Runner,
    user: Any,
    workspace: Workspace,
    selection: Selection,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A failure after command allocation rolls back every earlier write."""
    service = WorkspaceConfigurationService()
    create_task = TaskRepository.create
    workspace_id = uuid.uuid4() if operation == "create" else workspace.id

    def fail_after_task(**kwargs: Any) -> None:
        create_task(**kwargs)
        raise RuntimeError("late allocation failure")

    monkeypatch.setattr(TaskRepository, "create", fail_after_task)
    with pytest.raises(RuntimeError, match="late allocation failure"):
        if operation == "create":
            _create(service, runner, user, selection, workspace_id)
        else:
            _, credential, plugin = selection
            service.update(
                workspace_id=workspace.id,
                user=user,
                organization_id=runner.organization_id,
                credentials=[credential],
                plugin_ids=[plugin.id],
                name="Changed",
                qemu_values=(4, 4096, 60),
                desktop_values=(1280, 720),
            )

    assert not Task.objects.filter(workspace_id=workspace_id).exists()
    assert not LifecycleCommand.objects.exists()
    assert not WorkspacePluginActivation.objects.filter(
        workspace_id=workspace_id
    ).exists()
    assert not WorkspaceProcess.objects.filter(workspace_id=workspace_id).exists()
    if operation == "create":
        assert not Workspace.objects.filter(pk=workspace_id).exists()
    else:
        fresh = Workspace.objects.get(pk=workspace.id)
        for field in (
            "name",
            "qemu_vcpus",
            "qemu_memory_mb",
            "qemu_disk_size_gb",
            "desktop_width",
            "desktop_height",
        ):
            assert getattr(fresh, field) == getattr(workspace, field)
        assert not fresh.credentials.exists()


@pytest.mark.parametrize("boundary", ["owner", "organization", "capture", "task"])
def test_update_rejects_inaccessible_or_reserved_workspace(
    workspace: Workspace, user: Any, runner: Runner, boundary: str
) -> None:
    """Authorization and lifecycle fences precede configuration writes."""
    org_id = runner.organization_id
    expected = NotFoundError
    if boundary == "owner":
        workspace.created_by = get_user_model().objects.create_user(
            email="other-config-owner@example.test"
        )
        workspace.save(update_fields=["created_by"])
    elif boundary == "organization":
        org_id = uuid.uuid4()
    elif boundary == "capture":
        workspace.active_operation = "capturing"
        workspace.save(update_fields=["active_operation"])
        expected = ConflictError
    else:
        workspace.current_task = Task.objects.create(
            runner=runner, type="stop_workspace"
        )
        workspace.save(update_fields=["current_task"])
        expected = ConflictError

    with pytest.raises(expected):
        WorkspaceConfigurationService().update(
            workspace_id=workspace.id,
            user=user,
            organization_id=org_id,
            credentials=None,
            plugin_ids=None,
            name="Forbidden",
        )
    workspace.refresh_from_db()
    assert workspace.name == "Fixture Workspace"


@pytest.mark.parametrize("change", ["disappeared", "reserved"])
def test_update_rechecks_workspace_after_acquiring_runner_lock(
    workspace: Workspace,
    user: Any,
    runner: Runner,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A stale initial read cannot authorize a write after the runner lock."""
    get_workspace = WorkspaceRepository.get_by_id

    def changed_workspace(
        workspace_id: uuid.UUID, *, lock: bool = False
    ) -> Workspace | None:
        if lock:
            if change == "disappeared":
                return None
            Workspace.objects.filter(pk=workspace_id).update(
                active_operation="capturing"
            )
        return get_workspace(workspace_id, lock=lock)

    monkeypatch.setattr(WorkspaceRepository, "get_by_id", changed_workspace)
    error = NotFoundError if change == "disappeared" else ConflictError
    with pytest.raises(error):
        WorkspaceConfigurationService().update(
            workspace_id=workspace.id,
            user=user,
            organization_id=runner.organization_id,
            credentials=None,
            plugin_ids=None,
            name="Stale write",
        )
    workspace.refresh_from_db()
    assert workspace.name == "Fixture Workspace"
