"""Characterize recreate admission, atomic child progression and recovery."""

from unittest.mock import Mock

import pytest
from django.db import connection

from apps.runners import recreate_repository
from apps.runners.models import (
    ImageDeletionRequest,
    LifecycleCommand,
    Runner,
    Task,
    WorkspaceRecreateRequest,
)
from apps.runners.operations import OperationRepository
from apps.runners.phase_children import UNKNOWN_CHILD_DIAGNOSTIC
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService
from apps.runners.services.recreate import RecreateService
from apps.runners.tests.conftest import has_busy_agent
from apps.runners.tests.test_capture_pipeline import result
from apps.runners.tests.test_image_versions import drive, make_line, qemu_workspace
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db(transaction=True)


def test_busy_check_runs_under_runner_lock(
    runner: Runner, user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent admission and recreation must serialize on the same runner lock."""
    _, (image,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, image)
    lock = Mock(wraps=recreate_repository.lock_runner)
    monkeypatch.setattr(recreate_repository, "lock_runner", lock)

    def busy(workspace_id) -> bool:
        assert workspace_id == ws.id
        assert connection.in_atomic_block
        lock.assert_called_once_with(runner.id)
        return True

    with pytest.raises(ConflictError, match="An agent is active"):
        RecreateService.allocate(ws.id, image.id, has_busy_agent=busy)
    assert not WorkspaceRecreateRequest.objects.exists()
    assert not Task.objects.exists()


@pytest.mark.parametrize("blocker", ["deletion", "pending", "in_progress"])
def test_recreate_admission_rejects_reserved_workspaces(
    runner: Runner, user, blocker: str
) -> None:
    """Deletion approvals and credential injection block destructive recreation."""
    _, (image,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, image)
    if blocker == "deletion":
        ImageDeletionRequest.objects.create(
            organization=runner.organization,
            target_type="image",
            target_id=image.id,
            mode="force",
            approval={"workspaces": [{"id": str(ws.id)}]},
        )
        message = "reserved by approved image deletion"
    else:
        Task.objects.create(
            runner=runner, workspace=ws, type="inject_credentials", status=blocker
        )
        message = "credentials are synchronizing"
    with pytest.raises(ConflictError, match=message):
        RecreateService.allocate(ws.id, image.id, has_busy_agent=has_busy_agent)
    ws.refresh_from_db()
    assert ws.pending_base_image_instance_id is None
    assert not WorkspaceRecreateRequest.objects.exists()


def test_failed_initial_command_rolls_back_disk_target_and_request(
    runner: Runner, user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk growth, image pinning and the first command must commit together."""
    _, (base, target) = make_line(runner, user, ("ready", "ready"))
    ws = qemu_workspace(runner, user, base)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("command preparation failed")

    monkeypatch.setattr(OperationRepository, "prepare", fail)
    with pytest.raises(RuntimeError, match="command preparation failed"):
        RecreateService.allocate(
            ws.id, target.id, has_busy_agent=has_busy_agent, qemu_disk_size_gb=40
        )
    ws.refresh_from_db()
    assert ws.qemu_disk_size_gb == 20
    assert ws.base_image_instance_id == base.id
    assert ws.pending_base_image_instance_id is None
    assert ws.current_task_id is None and ws.active_operation is None
    assert not WorkspaceRecreateRequest.objects.exists()
    assert not Task.objects.exists()
    assert not LifecycleCommand.objects.exists()


def test_failed_next_child_rolls_back_version_switch_and_retries(
    runner: Runner, user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery's remove fallback and the create command are one transaction."""
    _, (base, target) = make_line(runner, user, ("ready", "ready"))
    ws = qemu_workspace(runner, user, base)
    _, remove, request = RecreateService.allocate(
        ws.id, target.id, has_busy_agent=has_busy_agent
    )
    TaskRepository.complete(remove)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("next command preparation failed")

    with monkeypatch.context() as patch:
        patch.setattr(OperationRepository, "prepare", fail)
        with pytest.raises(RuntimeError, match="next command preparation failed"):
            RecreateService.advance(request.id)
    request.refresh_from_db()
    ws.refresh_from_db()
    assert request.phase == "remove" and request.child_id == remove.id
    assert ws.base_image_instance_id == base.id
    assert ws.pending_base_image_instance_id == target.id
    assert ws.status == "running" and ws.active_operation == "updating"
    assert Task.objects.count() == LifecycleCommand.objects.count() == 1

    assert RecreateService.advance(request.id) is None
    request.refresh_from_db()
    ws.refresh_from_db()
    assert request.phase == "create" and request.child.type == (
        "create_workspace_from_image_artifact"
    )
    assert ws.base_image_instance_id == target.id
    assert ws.pending_base_image_instance_id is None and ws.status == "creating"
    assert Task.objects.count() == LifecycleCommand.objects.count() == 2


def test_unknown_child_is_fenced_and_not_replaced(runner: Runner, user) -> None:
    """Repeated recovery retains the child and emits its intervention only once."""
    _, (image,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, image)
    _, remove, request = RecreateService.allocate(
        ws.id, image.id, has_busy_agent=has_busy_agent
    )
    OperationRepository.intervene(remove, "Runner disconnected before result")
    assert RecreateService.advance(request.id) == {
        "workspace_id": str(ws.id),
        "phase": "intervention",
        "diagnostic": UNKNOWN_CHILD_DIAGNOSTIC,
    }
    assert RecreateService.advance(request.id) is None
    ws.refresh_from_db()
    request.refresh_from_db()
    assert request.phase == "remove" and request.child_id == remove.id
    assert ws.current_task_id == remove.id and ws.active_operation is None
    assert LifecycleCommand.objects.get(task=remove).phase == "intervention"
    assert Task.objects.count() == 1


def test_stopped_recreate_finishes_once(
    service: RunnerService, runner: Runner, user
) -> None:
    """Successful stop returns the existing final notification and is idempotent."""
    _, (image,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, image, status="stopped")
    _, _, request = RecreateService.allocate(
        ws.id, image.id, has_busy_agent=has_busy_agent
    )
    drive(service, runner, request, "workspace:removed", result="deleted")
    drive(service, runner, request, "workspace:created", status="running")
    assert drive(
        service, runner, request, "workspace:stopped", credentials_present=False
    ) == [
        {
            "workspace_id": str(ws.id),
            "phase": "completed",
            "status": "stopped",
            "diagnostic": "",
        }
    ]
    assert RecreateService.advance(request.id) is None
    assert RecreateService.tick() == []
    assert Task.objects.count() == 3


def test_recovery_continues_after_one_request_fails(
    service: RunnerService, runner: Runner, user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient coordinator failure must not block other reservations."""
    _, (image,) = make_line(runner, user)
    requests = []
    for _ in range(2):
        ws = qemu_workspace(runner, user, image)
        _, task, request = RecreateService.allocate(
            ws.id, image.id, has_busy_agent=has_busy_agent
        )
        assert result(service, runner, task, "workspace:error", error="disk busy")
        requests.append(request)
    advance = RecreateService.advance

    def progress(request_id) -> dict | None:
        if request_id == requests[0].id:
            raise RuntimeError("database temporarily unavailable")
        return advance(request_id)

    monkeypatch.setattr(RecreateService, "advance", progress)
    [notification] = RecreateService.tick()
    assert notification["workspace_id"] == str(requests[1].workspace_id)
    assert notification["phase"] == notification["status"] == "failed"
    for request, phase in zip(requests, ["remove", "failed"]):
        request.refresh_from_db()
        assert request.phase == phase
