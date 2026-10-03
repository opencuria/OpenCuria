"""Durable explicit stop/capture/resume child progression."""

import pytest

from apps.runners.capture_repository import CaptureRepository
from apps.runners.models import CaptureRequest, LifecycleCommand
from apps.runners.operations import OperationRepository, apply_result
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


def result(service, runner, task, event, **extra):
    row = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(
        row, {"task_id": str(task.id), "workspace_id": str(task.workspace_id), **extra}
    )
    return apply_result(service, str(runner.id), event, data)


def test_automatic_stop_capture_resume(service, runner, workspace):
    workspace.runtime_type = "qemu"
    workspace.credentials_present = True
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture")
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
    _, task = CaptureRepository.allocate(ws.id, "capture")
    OperationRepository.intervene(task, "Unknown capture deadline")
    CaptureRepository.tick()
    req = CaptureRequest.objects.get(workspace=ws)
    ws.refresh_from_db()
    assert req.phase == "capture" and ws.current_task_id == task.id
    assert "Unknown" in req.diagnostic


def test_safe_capture_failure_resumes_but_unknown_failure_does_not(
    service, runner, workspace
):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture")
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
    _, task = CaptureRepository.allocate(ws.id, "capture")
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
    _, stop = CaptureRepository.allocate(workspace.id, "capture")
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
    _, stop = CaptureRepository.allocate(workspace.id, "capture")
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
            CaptureRepository.allocate(workspace.id, "overlap")
    CaptureRepository.tick()
    assert CaptureRequest.objects.get(workspace=workspace).phase == "stop"


def test_capture_resume_child_retains_exact_qemu_resources(service, runner, workspace):
    workspace.runtime_type = "qemu"
    workspace.qemu_vcpus = 1
    workspace.qemu_memory_mb = 1024
    workspace.qemu_disk_size_gb = 20
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture")
    assert result(service, runner, stop, "workspace:stopped", credentials_present=False)
    CaptureRepository.tick()
    req = CaptureRequest.objects.get(workspace=workspace)
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
    payload = OperationRepository.delivery_payload(
        LifecycleCommand.objects.get(task=req.child)
    )
    assert req.phase == "resume"
    assert [
        payload[k] for k in ("qemu_vcpus", "qemu_memory_mb", "qemu_disk_size_gb")
    ] == [1, 1024, 20]
    assert result(
        service, runner, req.child, "workspace:resumed", credentials_present=False
    )
    CaptureRepository.tick()
    req.refresh_from_db()
    workspace.refresh_from_db()
    assert req.phase == "completed" and not req.diagnostic
    assert workspace.current_task_id is None and workspace.status == "running"


def test_full_success_holds_parent_until_recovery(
    service,
    sio_mock,
    runner,
    workspace,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    import uuid
    from unittest.mock import Mock

    from asgiref.sync import async_to_sync

    from apps.runners.models import ImageInstance, Task
    from apps.runners.repositories import TaskRepository, WorkspaceRepository
    from apps.runners.services import RunnerService
    from apps.runners.services.recovery import RecoveryService

    workspace.runtime_type = "qemu"
    workspace.credentials_present = True
    workspace.save()
    frontend = Mock()
    monkeypatch.setattr(RunnerService, "_forward_to_frontend", frontend)
    with django_capture_on_commit_callbacks(execute=True):
        _, stop = async_to_sync(service.create_image_artifact)(workspace.id, "capture")
    request = CaptureRequest.objects.get(workspace=workspace)
    assert request.prior_running and request.phase == "stop"
    assert request.child_id == stop.id
    for phase, event, extra, status in [
        ("stop", "workspace:stopped", {"credentials_present": False}, "stopped"),
        (
            "capture",
            "image_artifact:created",
            {
                "image_artifact_id": "/capture.qcow2",
                "name": "capture",
                "size_bytes": 123,
            },
            "stopped",
        ),
        ("resume", "workspace:resumed", {"credentials_present": False}, "running"),
    ]:
        request.refresh_from_db()
        assert request.phase == phase
        workspace.refresh_from_db()
        assert workspace.current_task_id == request.child_id
        assert workspace.active_operation == "capturing_image"
        with django_capture_on_commit_callbacks(execute=True):
            assert result(service, runner, request.child, event, **extra)
        workspace.refresh_from_db()
        request.child.refresh_from_db()
        assert request.child.status == "completed"
        assert workspace.status == status
        assert workspace.current_task_id is None
        assert workspace.active_operation == "capturing_image"
        WorkspaceRepository.update_active_operation(workspace, None)
        assert workspace.active_operation == "capturing_image"
        before = {
            f.attname: getattr(workspace, f.attname)
            for f in workspace._meta.concrete_fields
        }
        counts = (
            Task.objects.count(),
            ImageInstance.objects.count(),
            CaptureRequest.objects.count(),
            LifecycleCommand.objects.count(),
        )
        for method, kwargs in [
            (service.stop_workspace, {}),
            (service.remove_workspace, {}),
            (service.resume_workspace, {}),
            (service.rename_workspace, {"name": "denied"}),
            (service.update_workspace, {"qemu_vcpus": 2}),
            (service.update_workspace, {"credentials": []}),
            (service.create_image_artifact, {"name": "overlap"}),
        ]:
            with pytest.raises(ConflictError):
                async_to_sync(method)(workspace.id, **kwargs)
        for kind in [
            "stop_workspace",
            "remove_workspace",
            "resume_workspace",
            "update_workspace",
            "create_image_artifact",
            "create_workspace",
            "create_workspace_from_image_artifact",
        ]:
            with pytest.raises(ConflictError):
                TaskRepository.create(
                    task_id=uuid.uuid4(),
                    runner=runner,
                    task_type=kind,
                    workspace=workspace,
                )
        workspace.refresh_from_db()
        assert {
            f.attname: getattr(workspace, f.attname)
            for f in workspace._meta.concrete_fields
        } == before
        assert counts == (
            Task.objects.count(),
            ImageInstance.objects.count(),
            CaptureRequest.objects.count(),
            LifecycleCommand.objects.count(),
        )
        request.refresh_from_db()
        assert not request.resume_suppressed
        operations = [
            c.args[1]["active_operation"]
            for c in frontend.call_args_list
            if c.args[0] == "workspace:operation_changed"
        ]
        assert operations and set(operations) == {"capturing_image"}
        with django_capture_on_commit_callbacks(execute=True):
            async_to_sync(RecoveryService().tick)(sio_mock)
    request.refresh_from_db()
    workspace.refresh_from_db()
    assert request.phase == "completed" and not request.diagnostic
    assert request.image.status == "ready"
    assert workspace.status == "running" and workspace.current_task_id is None
    assert workspace.active_operation is None
    operations = [
        c.args[1]["active_operation"]
        for c in frontend.call_args_list
        if c.args[0] == "workspace:operation_changed"
    ]
    assert operations[-1] is None and operations.count(None) == 1
    assert Task.objects.filter(workspace=workspace).count() == 3


def test_stopped_capture_final_clear_without_resume(service, runner, stopped_workspace):
    from apps.runners.models import Task

    ws = stopped_workspace
    ws.runtime_type = "qemu"
    ws.save()
    _, child = CaptureRepository.allocate(ws.id, "capture")
    request = CaptureRequest.objects.get(workspace=ws)
    assert not request.prior_running and request.phase == "capture"
    assert result(
        service,
        runner,
        child,
        "image_artifact:created",
        image_artifact_id="/capture.qcow2",
        name="capture",
        size_bytes=123,
    )
    ws.refresh_from_db()
    assert ws.current_task_id is None and ws.active_operation == "capturing_image"
    assert CaptureRepository.tick() == [
        {"workspace_id": str(ws.id), "phase": "completed", "diagnostic": ""}
    ]
    assert CaptureRepository.tick() == []
    ws.refresh_from_db()
    assert ws.status == "stopped" and ws.active_operation is None
    assert Task.objects.filter(workspace=ws).count() == 1


@pytest.mark.parametrize(
    "capture_failed,restart_failed",
    [
        (True, False),
        (False, True),
        (True, True),
    ],
)
def test_safe_failure_final_diagnostics_and_recovery_event(
    service,
    sio_mock,
    runner,
    workspace,
    monkeypatch,
    django_capture_on_commit_callbacks,
    capture_failed,
    restart_failed,
):
    from unittest.mock import Mock

    from asgiref.sync import async_to_sync

    from apps.runners.services import RunnerService
    from apps.runners.services.recovery import RecoveryService

    workspace.runtime_type = "qemu"
    workspace.save()
    _, stop = CaptureRepository.allocate(workspace.id, "capture")
    assert result(service, runner, stop, "workspace:stopped", credentials_present=False)
    assert CaptureRepository.tick() == []
    request = CaptureRequest.objects.get(workspace=workspace)
    assert result(
        service,
        runner,
        request.child,
        "image_artifact:failed" if capture_failed else "image_artifact:created",
        **(
            {"error": "safe refusal"}
            if capture_failed
            else {
                "image_artifact_id": "/capture.qcow2",
                "name": "capture",
                "size_bytes": 123,
            }
        ),
    )
    assert CaptureRepository.tick() == []
    request.refresh_from_db()
    assert request.phase == "resume"
    if capture_failed:
        assert "safe refusal" in request.diagnostic
    assert result(
        service,
        runner,
        request.child,
        "workspace:error" if restart_failed else "workspace:resumed",
        **(
            {"error": "start failed"}
            if restart_failed
            else {"credentials_present": False}
        ),
    )
    workspace.refresh_from_db()
    assert workspace.current_task_id is None
    assert workspace.active_operation == "capturing_image"
    frontend = Mock()
    monkeypatch.setattr(RunnerService, "_forward_to_frontend", frontend)
    with django_capture_on_commit_callbacks(execute=True):
        async_to_sync(RecoveryService().tick)(sio_mock)
    request.refresh_from_db()
    workspace.refresh_from_db()
    assert request.phase == ("failed" if capture_failed else "completed")
    assert request.image.status == ("failed" if capture_failed else "ready")
    assert (
        "restart failed" if restart_failed else "safe refusal"
    ) in request.diagnostic
    if not capture_failed:
        assert request.image.runner_ref == "/capture.qcow2"
    assert workspace.current_task_id is None and workspace.active_operation is None
    assert workspace.status == ("stopped" if restart_failed else "running")
    frontend.assert_any_call(
        "workspace:operation_changed",
        {
            "workspace_id": str(workspace.id),
            "active_operation": None,
            "intervention_required": False,
            "lifecycle_diagnostic": "",
        },
        str(workspace.id),
    )
    frontend.assert_any_call(
        "workspace:error",
        {"workspace_id": str(workspace.id), "error": request.diagnostic},
        str(workspace.id),
    )
