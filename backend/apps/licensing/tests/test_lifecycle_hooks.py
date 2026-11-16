"""Admission inside start retries and capture replacements."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from apps.licensing.models import WindowsLicensePolicy, WindowsWorkspaceLicense
from apps.licensing.tests.factories import create, product_key, settle
from apps.runners.disposition_repository import DispositionRepository
from apps.runners.enums import TaskType, WorkspaceStatus
from apps.runners.models import (
    CaptureRequest,
    LifecycleCommand,
    Task,
    WorkspaceRecreateRequest,
)
from apps.runners.operations import OperationRepository
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService
from apps.runners.services.capture import CaptureService
from apps.runners.services.disposition import DispositionService
from apps.runners.services.operation_results import apply_result
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


def _retry(runner, task: Task) -> dict:
    """Retry a finished start through the operator disposition."""
    row = LifecycleCommand.objects.get(task=task)
    evidence = {
        "status": "terminal",
        "instance_id": "process",
        "execution_finished": True,
        "outcome_known": True,
        "quiescent": True,
        "event": "workspace:error",
        "workspace_status": "stopped",
        "identity": OperationRepository.envelope(row, {}),
    }
    return DispositionService._dispose(
        DispositionRepository.get(runner.organization_id, task.id), evidence, "retry"
    )


def test_start_retry_is_admitted_again(runner, admin, member, licenses):
    WindowsLicensePolicy.objects.create(
        organization=runner.organization, mode="unlicensed"
    )
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)
    WindowsLicensePolicy.objects.filter(organization=runner.organization).update(
        mode="per_device"
    )

    start = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type=TaskType.RESUME_WORKSPACE,
    )

    with pytest.raises(ConflictError) as error:
        _retry(runner, start)
    assert error.value.code == "windows_license_unavailable"
    assert Task.objects.filter(type="resume_workspace").count() == 1

    licenses.add_key(
        org_id=runner.organization_id, user=admin, product_key=product_key(1)
    )
    result = _retry(runner, start)

    assert Task.objects.filter(pk=result["operation_id"]).exists()
    assert WindowsWorkspaceLicense.objects.get(workspace=workspace).kind == "device_key"


def _report(service, runner, task, event: str, **extra) -> None:
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(
        command,
        {"task_id": str(task.id), "workspace_id": str(task.workspace_id), **extra},
    )
    assert apply_result(service, str(runner.id), event, data)


def test_capture_replacement_waits_for_a_running_slot(runner, member):
    """A capture keeps the source on its version when it cannot boot again."""
    service = RunnerService(sio_server=AsyncMock())
    WindowsLicensePolicy.objects.create(
        organization=runner.organization, mode="unlicensed"
    )
    source = settle(create(runner, member), WorkspaceStatus.RUNNING)
    other = settle(create(runner, member), WorkspaceStatus.STOPPED)
    WindowsLicensePolicy.objects.filter(organization=runner.organization).update(
        max_running=1
    )
    base = source.base_image_instance_id
    _, stop = CaptureService.allocate(
        source.id, "Snapshot", has_busy_agent=lambda _: False
    )
    _report(service, runner, stop, "workspace:stopped", credentials_present=False)
    CaptureService.tick()
    request = CaptureRequest.objects.get(workspace=source)
    assert request.phase == "capture"
    # Another Windows workspace takes the only running slot meanwhile.
    settle(other, WorkspaceStatus.RUNNING)

    _report(
        service,
        runner,
        request.child,
        "image_artifact:created",
        image_artifact_id="/captures/snapshot.qcow2",
        name="Snapshot",
        size_bytes=1,
    )
    CaptureService.tick()

    request.refresh_from_db()
    source.refresh_from_db()
    assert request.phase == "completed"
    assert "maximum number of running Windows workspaces" in request.diagnostic
    assert "remains stopped" in request.diagnostic
    assert source.status == WorkspaceStatus.STOPPED
    assert source.base_image_instance_id == base
    assert not WorkspaceRecreateRequest.objects.filter(workspace=source).exists()
    assert WindowsWorkspaceLicense.objects.get(workspace=source).kind == "unlicensed"
