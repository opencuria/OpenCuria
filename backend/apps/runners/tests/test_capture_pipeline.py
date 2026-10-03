"""Durable explicit stop/capture/resume child progression."""

import pytest
from apps.runners.capture_repository import CaptureRepository
from apps.runners.models import CaptureRequest, LifecycleCommand
from apps.runners.operations import OperationRepository, apply_result
from apps.runners.tests.test_services import service, sio_mock
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


def result(service, runner, task, event, **extra):
    row = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(
        row, {"task_id": str(task.id), "workspace_id": str(task.workspace_id), **extra}
    )
    return apply_result(service, str(runner.id), event, data)


def test_explicit_stop_capture_resume(service, runner, workspace):
    workspace.runtime_type = "qemu"
    workspace.credentials_present = True
    workspace.save()
    with pytest.raises(ConflictError):
        CaptureRepository.allocate(workspace.id, "capture", False)
    _, stop = CaptureRepository.allocate(workspace.id, "capture", True)
    req = CaptureRequest.objects.get(workspace=workspace)
    assert result(service, runner, stop, "workspace:stopped", credentials_present=False)
    CaptureRepository.tick()
    req.refresh_from_db()
    assert req.phase == "capture"
    assert LifecycleCommand.objects.get(task=req.child).payload[
        "image_instance_id"
    ] == str(req.image_id)
    assert result(
        service,
        runner,
        req.child,
        "image_artifact:created",
        image_artifact_id="/capture.qcow2",
        name="capture",
        size_bytes=123,
    )
    CaptureRepository.tick()
    req.refresh_from_db()
    assert req.phase == "resume"
    assert result(service, runner, req.child, "workspace:error", error="start failed")
    CaptureRepository.tick()
    req.refresh_from_db()
    assert req.phase == "completed"
    assert req.image.status == "ready"
    assert "restart failed" in req.diagnostic


def test_unknown_capture_fences_and_suppressed_resume(
    service, runner, stopped_workspace
):
    ws = stopped_workspace
    ws.runtime_type = "qemu"
    ws.save()
    _, task = CaptureRepository.allocate(ws.id, "capture", False)
    OperationRepository.intervene(task, "Unknown capture deadline")
    CaptureRepository.tick()
    req = CaptureRequest.objects.get(workspace=ws)
    ws.refresh_from_db()
    assert req.phase == "capture" and ws.current_task_id == task.id
    assert "Unknown" in req.diagnostic


def test_later_stop_suppresses_approved_restart(service, runner, workspace):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, task = CaptureRepository.allocate(workspace.id, "capture", True)
    assert result(service, runner, task, "workspace:stopped", credentials_present=False)
    CaptureRepository.tick()
    req = CaptureRequest.objects.get(workspace=workspace)
    CaptureRepository.suppress_resume(workspace.id)
    assert result(
        service, runner, req.child, "image_artifact:failed", error="safe refusal"
    )
    CaptureRepository.tick()
    req.refresh_from_db()
    assert req.phase == "failed"


def test_safe_capture_failure_resumes_but_unknown_failure_does_not(
    service, runner, workspace
):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture", True)
    assert result(service, runner, stop, "workspace:stopped", credentials_present=False)
    CaptureRepository.tick()
    req = CaptureRequest.objects.get(workspace=workspace)
    assert result(
        service, runner, req.child, "image_artifact:failed", error="safe refusal"
    )
    CaptureRepository.tick()
    req.refresh_from_db()
    assert req.phase == "resume"
    assert req.image.status == "failed"


def test_journal_interruption_failure_retains_fence(service, runner, stopped_workspace):
    ws = stopped_workspace
    ws.runtime_type = "qemu"
    ws.save()
    _, task = CaptureRepository.allocate(ws.id, "capture", False)
    assert not result(
        service,
        runner,
        task,
        "image_artifact:failed",
        error="Manual intervention required",
    )
    CaptureRepository.tick()
    ws.refresh_from_db()
    assert ws.current_task_id == task.id
    assert LifecycleCommand.objects.get(task=task).phase == "intervention"


def test_retirement_after_clean_stop_preserves_restart_approval(
    service, runner, workspace
):
    from apps.runners.models import ImageInstance

    base = ImageInstance.objects.create(
        runner=runner, runtime_type="qemu", name="base", status="ready"
    )
    workspace.runtime_type = "qemu"
    workspace.base_image_instance = base
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture", True)
    assert result(service, runner, stop, "workspace:stopped", credentials_present=False)
    base.status = "retired"
    base.save()
    CaptureRepository.tick()
    req = CaptureRequest.objects.get(workspace=workspace)
    assert req.phase == "resume"
    assert req.image.status == "failed"
    assert req.child.type == "resume_workspace"


def test_unknown_handler_failure_and_delayed_unknown_result_hold_fence(
    service, runner, workspace
):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture", True)
    for _ in range(2):
        assert not result(
            service,
            runner,
            stop,
            "workspace:error",
            error="Handler failed",
            execution_finished=True,
            outcome_known=False,
        )
        workspace.refresh_from_db()
        assert workspace.current_task_id == stop.id
        assert LifecycleCommand.objects.get(task=stop).phase == "intervention"
        with pytest.raises(ConflictError):
            CaptureRepository.allocate(workspace.id, "overlap", True)
    CaptureRepository.tick()
    assert CaptureRequest.objects.get(workspace=workspace).phase == "stop"


def test_retirement_race_explicit_stop_suppresses_restart(service, runner, workspace):
    from apps.runners.models import ImageInstance

    base = ImageInstance.objects.create(
        runner=runner, runtime_type="qemu", name="base", status="ready"
    )
    workspace.runtime_type = "qemu"
    workspace.base_image_instance = base
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture", True)
    assert result(service, runner, stop, "workspace:stopped", credentials_present=False)
    CaptureRepository.suppress_resume(workspace.id)
    base.status = "retired"
    base.save()
    CaptureRepository.tick()
    req = CaptureRequest.objects.get(workspace=workspace)
    assert req.phase == "failed"
    assert req.child_id == stop.id


def test_capture_resume_child_retains_exact_qemu_resources(service, runner, workspace):
    workspace.runtime_type = "qemu"
    workspace.qemu_vcpus = 1
    workspace.qemu_memory_mb = 1024
    workspace.qemu_disk_size_gb = 20
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture", True)
    assert result(service, runner, stop, "workspace:stopped", credentials_present=False)
    CaptureRepository.tick()
    req = CaptureRequest.objects.get(workspace=workspace)
    assert result(service, runner, req.child, "image_artifact:created",
                  image_artifact_id="/capture.qcow2", name="capture", size_bytes=123)
    CaptureRepository.tick()
    req.refresh_from_db()
    payload = OperationRepository.delivery_payload(LifecycleCommand.objects.get(task=req.child))
    assert req.phase == "resume"
    assert [payload[k] for k in ("qemu_vcpus", "qemu_memory_mb", "qemu_disk_size_gb")] == [1, 1024, 20]
    assert result(service, runner, req.child, "workspace:resumed", credentials_present=False)
    CaptureRepository.tick()
    req.refresh_from_db()
    workspace.refresh_from_db()
    assert req.phase == "completed" and not req.diagnostic
    assert workspace.current_task_id is None and workspace.status == "running"
