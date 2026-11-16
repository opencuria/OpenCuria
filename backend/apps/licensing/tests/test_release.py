"""Seats are freed only when a workspace deletion is confirmed."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from apps.licensing.models import WindowsLicensePolicy, WindowsWorkspaceLicense
from apps.licensing.tests.factories import create, product_key, settle
from apps.runners.enums import TaskType, WorkspaceStatus
from apps.runners.models import LifecycleCommand, Task, Workspace
from apps.runners.operations import OperationRepository
from apps.runners.repositories import TaskRepository, WorkspaceRepository
from apps.runners.services import RunnerService
from apps.runners.services.operation_results import apply_result
from apps.runners.services.recreate import RecreateService
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


@pytest.fixture
def service() -> RunnerService:
    return RunnerService(sio_server=AsyncMock())


@pytest.fixture
def seat(runner, admin, licenses):
    """A per-device pool with exactly one seat."""
    licenses.add_key(
        org_id=runner.organization_id, user=admin, product_key=product_key(1)
    )


def _remove_result(service, runner, workspace, task: Task, result: str) -> bool:
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(
        command,
        {"task_id": str(task.id), "workspace_id": str(workspace.id), "result": result},
    )
    return apply_result(service, str(runner.id), "workspace:removed", data)


def _delete(service, runner, workspace, result: str = "deleted") -> None:
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type=TaskType.REMOVE_WORKSPACE,
    )
    WorkspaceRepository.mark_deleting(workspace.id)
    assert _remove_result(service, runner, workspace, task, result)


def _held(workspace) -> bool:
    return WindowsWorkspaceLicense.objects.filter(workspace=workspace).exists()


def test_confirmed_delete_frees_the_seat(service, runner, member, seat):
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)
    with pytest.raises(ConflictError):
        create(runner, member)

    _delete(service, runner, workspace)

    workspace.refresh_from_db()
    assert workspace.status == WorkspaceStatus.DELETED
    assert not _held(workspace)
    assert _held(create(runner, member))


def test_unconfirmed_delete_keeps_the_seat(service, runner, member, seat):
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)

    _delete(service, runner, workspace, result="busy")

    workspace.refresh_from_db()
    assert workspace.status == WorkspaceStatus.DELETE_FAILED
    assert _held(workspace)


def test_recreate_removal_keeps_the_seat(service, runner, member, seat):
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)
    _, remove, _ = RecreateService.allocate(
        workspace.id, workspace.base_image_instance_id, has_busy_agent=lambda _: False
    )

    assert _remove_result(service, runner, workspace, remove, "deleted")

    workspace.refresh_from_db()
    assert workspace.status == WorkspaceStatus.CREATING
    assert _held(workspace)


def test_heartbeat_observed_removal_frees_the_seat(runner, member, seat):
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)

    assert WorkspaceRepository.reconcile_heartbeat_status(
        workspace, WorkspaceStatus.DELETED, runner_sid=runner.sid
    )

    assert not _held(workspace)


def test_heartbeat_observations_other_than_removal_keep_the_seat(runner, member, seat):
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)

    assert WorkspaceRepository.reconcile_heartbeat_status(
        workspace, WorkspaceStatus.RUNNING, runner_sid=runner.sid
    )

    assert _held(workspace)


def test_safety_net_frees_only_deleted_workspaces(runner, member, licenses):
    WindowsLicensePolicy.objects.create(
        organization=runner.organization, mode="unlicensed"
    )
    kept = {
        status: settle(create(runner, member), status)
        for status in (
            WorkspaceStatus.FAILED,
            WorkspaceStatus.PENDING_DELETION,
            WorkspaceStatus.DELETING,
            WorkspaceStatus.DELETE_FAILED,
        )
    }
    deleted = create(runner, member)
    # A deletion recorded without releasing (e.g. before licensing existed).
    Workspace.objects.filter(pk=deleted.pk).update(status=WorkspaceStatus.DELETED)

    assert licenses.release_deleted() == 1
    assert licenses.release_deleted() == 0

    assert not _held(deleted)
    assert all(_held(workspace) for workspace in kept.values())
