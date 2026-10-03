"""Deterministic interleavings at capture allocation and callback boundaries."""

import uuid
from unittest.mock import Mock

import pytest
from asgiref.sync import async_to_sync

from apps.harness.models import HarnessSession, QuestionRequest
from apps.harness.permissions.models import PermissionRequest
from apps.runners.capture_repository import CaptureRepository
from apps.runners.models import CaptureRequest, ImageInstance, LifecycleCommand, Task
from apps.runners.operations import OperationRepository, apply_result
from apps.runners.tests.test_capture_pipeline import result
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "session_kind", ["root", "child", "answer_waiter", "approval_waiter"]
)
def test_any_busy_session_refuses_capture_without_mutations(
    service,
    runner,
    workspace,
    session_kind,
):
    workspace.runtime_type = "qemu"
    workspace.save()
    root = HarnessSession.objects.create(
        workspace=workspace, organization_id=runner.organization_id, status="idle"
    )
    if session_kind == "child":
        busy = HarnessSession.objects.create(
            workspace=workspace,
            organization_id=runner.organization_id,
            parent=root,
            status="busy",
        )
    else:
        busy = root
        busy.status = "busy"
        busy.save(update_fields=["status"])
    waiter = None
    if session_kind == "answer_waiter":
        waiter = QuestionRequest.objects.create(
            organization_id=runner.organization_id,
            workspace_id=workspace.id,
            session_id=busy.id,
            questions=[{"question": "Continue?"}],
        )
    elif session_kind == "approval_waiter":
        waiter = PermissionRequest.objects.create(
            organization_id=runner.organization_id,
            workspace_id=workspace.id,
            session_id=busy.id,
            tool="bash",
            pattern="dangerous command",
        )
    # Waiting sessions deliberately retain busy; capture may not distinguish
    # provider execution from a pending user answer/approval.
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
    for allocate in [
        lambda: CaptureRepository.allocate(workspace.id, "capture"),
        lambda: async_to_sync(service.create_image_artifact)(workspace.id, "capture"),
    ]:
        with pytest.raises(ConflictError, match="agent is active or waiting"):
            allocate()
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
        busy.refresh_from_db()
        assert busy.status == "busy"
        if waiter:
            waiter.refresh_from_db()
            assert waiter.status == "pending" and waiter.resolved_at is None


@pytest.mark.parametrize(
    "foreign_field",
    [
        "runner_id",
        "workspace_id",
        "task_id",
        "attempt",
        "target",
        "operation_id",
    ],
)
def test_foreign_callback_does_not_release_capture_or_emit_events(
    service,
    runner,
    workspace,
    monkeypatch,
    django_capture_on_commit_callbacks,
    foreign_field,
):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, child = CaptureRepository.allocate(workspace.id, "capture")
    row = LifecycleCommand.objects.get(task=child)
    data = OperationRepository.envelope(
        row,
        {
            "task_id": str(child.id),
            "workspace_id": str(workspace.id),
            "credentials_present": False,
        },
    )
    data[foreign_field] = (
        row.attempt + 1 if foreign_field == "attempt" else str(uuid.uuid4())
    )
    frontend = Mock()
    monkeypatch.setattr(service, "_forward_to_frontend", frontend)
    with django_capture_on_commit_callbacks(execute=True):
        assert not apply_result(service, str(runner.id), "workspace:stopped", data)
    frontend.assert_not_called()
    workspace.refresh_from_db()
    child.refresh_from_db()
    assert workspace.current_task_id == child.id
    assert workspace.active_operation == "capturing_image"
    assert workspace.status == "running" and child.status == "pending"
    assert CaptureRepository.tick() == []
    assert CaptureRequest.objects.get(workspace=workspace).phase == "stop"


def test_unknown_stop_preserves_scrub_fence(service, runner, workspace):
    workspace.runtime_type = "qemu"
    workspace.credentials_present = True
    workspace.save()
    _, child = CaptureRepository.allocate(workspace.id, "capture")
    for attempt in range(2):
        assert not result(
            service,
            runner,
            child,
            "workspace:error",
            error="scrub outcome unknown",
            execution_finished=True,
            outcome_known=False,
        )
        diagnostic = "Unknown child outcome; exact journal reconciliation required"
        assert CaptureRepository.tick() == (
            [
                {
                    "workspace_id": str(workspace.id),
                    "phase": "intervention",
                    "diagnostic": diagnostic,
                }
            ]
            if attempt == 0
            else []
        )
        workspace.refresh_from_db()
        assert workspace.active_operation is None
        assert workspace.intervention_required
        assert (
            workspace.lifecycle_diagnostic == "Unknown outcome; intervention required"
        )
        assert workspace.current_task_id == child.id
        assert workspace.credentials_present
        assert workspace.status == "running"
        assert LifecycleCommand.objects.get(task=child).phase == "intervention"
        assert CaptureRequest.objects.get(workspace=workspace).phase == "stop"
        assert Task.objects.filter(workspace=workspace).count() == 1
        with pytest.raises(ConflictError):
            async_to_sync(service.stop_workspace)(workspace.id)
        with pytest.raises(ConflictError):
            async_to_sync(service.remove_workspace)(workspace.id)
        assert not CaptureRequest.objects.get(workspace=workspace).resume_suppressed


@pytest.mark.parametrize("task_status", ["pending", "in_progress"])
def test_admitted_credential_sync_blocks_capture(runner, workspace, task_status):
    """A reconciliation admitted first must settle before capture can own scrub."""
    from apps.runners.repositories import TaskRepository

    workspace.runtime_type = "qemu"
    workspace.save(update_fields=["runtime_type"])
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="inject_credentials",
    )
    Task.objects.filter(pk=task.id).update(status=task_status)
    with pytest.raises(ConflictError, match="credentials are synchronizing"):
        CaptureRepository.allocate(workspace.id, "capture")
    assert not CaptureRequest.objects.filter(workspace=workspace).exists()
    assert not ImageInstance.objects.filter(origin_workspace=workspace).exists()
    TaskRepository.complete(task)
    _, child = CaptureRepository.allocate(workspace.id, "capture")
    assert child.type == "stop_workspace"


def test_capture_admitted_first_blocks_delayed_credential_sync(runner, workspace):
    from apps.runners.repositories import TaskRepository

    workspace.runtime_type = "qemu"
    workspace.save(update_fields=["runtime_type"])
    _, child = CaptureRepository.allocate(workspace.id, "capture")
    with pytest.raises(ConflictError, match="capturing image"):
        TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=runner,
            workspace=workspace,
            task_type="inject_credentials",
        )
    assert Task.objects.filter(workspace=workspace).count() == 1
    workspace.refresh_from_db()
    assert workspace.current_task_id == child.id
