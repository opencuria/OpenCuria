"""Authenticated resume failures persist evidence without losing captured images."""

import uuid

import pytest

from apps.runners.capture_repository import CaptureRepository
from apps.runners.enums import TaskStatus, WorkspaceStatus
from apps.runners.models import CaptureRequest, LifecycleCommand
from apps.runners.operations import OperationRepository, apply_result
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock

pytestmark = pytest.mark.django_db


def resume_child(service, runner, workspace):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "evidence capture")
    send(service, runner, stop, "workspace:stopped", credentials_present=False)
    CaptureRepository.tick()
    request = CaptureRequest.objects.get(workspace=workspace)
    send(
        service,
        runner,
        request.child,
        "image_artifact:created",
        image_artifact_id="/capture.qcow2",
        name="capture",
        size_bytes=123,
    )
    CaptureRepository.tick()
    request.refresh_from_db()
    assert request.phase == "resume"
    return request


def send(service, runner, task, event, **extra):
    row = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(
        row,
        {
            "task_id": str(task.id),
            "workspace_id": str(task.workspace_id),
            **extra,
        },
    )
    return apply_result(service, str(runner.id), event, data)


@pytest.mark.parametrize(
    "state,present,status",
    [
        ("running", True, WorkspaceStatus.RUNNING),
        ("exited", False, WorkspaceStatus.STOPPED),
        ("stopped", False, WorkspaceStatus.STOPPED),
    ],
)
def test_known_resume_failure_persists_evidence(
    service, runner, workspace, state, present, status
):
    request = resume_child(service, runner, workspace)
    assert send(
        service,
        runner,
        request.child,
        "workspace:error",
        error="resume failed",
        observed_status=state,
        credentials_present=present,
        outcome_known=True,
        execution_finished=True,
    )
    workspace.refresh_from_db()
    assert workspace.status == status
    assert workspace.credentials_present is present
    assert workspace.current_task_id is None
    CaptureRepository.tick()
    request.refresh_from_db()
    assert request.phase == "completed"
    assert request.image.status == "ready"
    assert request.image.runner_ref == "/capture.qcow2"


def test_unknown_credentials_retains_fence_and_ready_image(service, runner, workspace):
    request = resume_child(service, runner, workspace)
    assert not send(
        service,
        runner,
        request.child,
        "workspace:error",
        error="injection failed",
        observed_status="running",
        outcome_known=False,
        execution_finished=True,
    )
    CaptureRepository.tick()
    request.refresh_from_db()
    workspace.refresh_from_db()
    assert workspace.current_task_id == request.child_id
    assert request.phase == "resume"
    assert request.image.status == "ready"
    assert LifecycleCommand.objects.get(task=request.child).phase == "intervention"


def test_unauthenticated_result_cannot_change_evidence(service, runner, workspace):
    request = resume_child(service, runner, workspace)
    row = LifecycleCommand.objects.get(task=request.child)
    data = OperationRepository.envelope(
        row,
        {
            "task_id": str(request.child_id),
            "workspace_id": str(workspace.id),
            "error": "failed",
            "observed_status": "running",
            "credentials_present": True,
            "outcome_known": True,
        },
    )
    data["runner_id"] = str(uuid.uuid4())
    assert not apply_result(service, str(runner.id), "workspace:error", data)
    workspace.refresh_from_db()
    request.child.refresh_from_db()
    assert workspace.status == WorkspaceStatus.STOPPED
    assert workspace.credentials_present is False
    assert workspace.current_task_id == request.child_id
    assert request.child.status != TaskStatus.FAILED


@pytest.mark.parametrize(
    "state,present,status",
    [
        ("running", True, WorkspaceStatus.RUNNING),
        ("running", False, WorkspaceStatus.RUNNING),
        ("exited", False, WorkspaceStatus.STOPPED),
        ("stopped", False, WorkspaceStatus.STOPPED),
    ],
)
def test_known_stop_failure_persists_actual_evidence(
    service, runner, workspace, state, present, status
):
    workspace.runtime_type = "qemu"
    workspace.credentials_present = not present
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "stop evidence")
    assert send(
        service,
        runner,
        stop,
        "workspace:error",
        error="stop failed",
        observed_status=state,
        credentials_present=present,
        outcome_known=True,
        execution_finished=True,
    )
    workspace.refresh_from_db()
    assert workspace.status == status
    assert workspace.credentials_present is present
    assert workspace.current_task_id is None


def test_unknown_stop_failure_does_not_release_capture_reservation(
    service, runner, workspace
):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "partial scrub")
    assert not send(
        service,
        runner,
        stop,
        "workspace:error",
        error="partial scrub failed",
        observed_status="running",
        outcome_known=False,
        execution_finished=True,
    )
    CaptureRepository.tick()
    workspace.refresh_from_db()
    request = CaptureRequest.objects.get(workspace=workspace)
    assert workspace.current_task_id == stop.id
    assert request.child_id == stop.id
    assert request.phase == "stop"
    assert LifecycleCommand.objects.get(task=stop).phase == "intervention"
