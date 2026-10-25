"""Characterize lifecycle callback atomicity and acknowledgement decisions."""

from __future__ import annotations

import uuid
from unittest.mock import Mock

import pytest

from apps.runners.models import LifecycleCommand, Task, Workspace
from apps.runners.operations import OperationRepository
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService
from apps.runners.services.operation_results import apply_result

pytestmark = pytest.mark.django_db


@pytest.fixture
def pending_result(runner, workspace) -> tuple[Task, dict]:
    """Allocate a bound stop result without delivering it to a runner."""
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    command = LifecycleCommand.objects.get(task=task)
    return task, OperationRepository.envelope(command, command.payload)


def test_callback_failure_rolls_back_state_journal_and_notifications(
    runner, workspace, pending_result, monkeypatch, django_capture_on_commit_callbacks
) -> None:
    """A failed callback cannot commit partial state or publish its queued events."""
    task, data = pending_result
    frontend = Mock()
    service = RunnerService(frontend_bus=frontend)
    callback = service.handle_workspace_stopped

    def fail_after_callback(task_id: str, workspace_id: str, runner_id: str) -> None:
        callback(task_id, workspace_id, runner_id)
        assert Task.objects.get(pk=task_id).status == "completed"
        assert Workspace.objects.get(pk=workspace_id).status == "stopped"
        assert LifecycleCommand.objects.get(task_id=task_id).phase == "result"
        raise RuntimeError("callback interrupted")

    monkeypatch.setattr(service, "handle_workspace_stopped", fail_after_callback)
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError, match="callback interrupted"):
            apply_result(service, str(runner.id), "workspace:stopped", data)

    task.refresh_from_db()
    workspace.refresh_from_db()
    command = LifecycleCommand.objects.get(task=task)
    assert task.status == "pending"
    assert workspace.status == "running"
    assert workspace.current_task_id == task.id
    assert workspace.active_operation == "stopping"
    assert command.phase == "delivery"
    assert command.heartbeat_at is None
    frontend.assert_not_called()


def test_nonterminal_callback_filters_and_normalizes_payload(
    runner, workspace, pending_result
) -> None:
    """Only accepted callback fields are forwarded; unfinished work is not ACKed."""
    task, data = pending_result
    received = []

    def callback(
        task_id: str, runner_id: str, artifact_id: str, runner_ref: str
    ) -> None:
        received.append((task_id, runner_id, artifact_id, runner_ref))

    service = Mock(handle_workspace_stopped=callback)
    data = {**data, "image_artifact_id": "/image.qcow2", "unused_field": True}
    assert not apply_result(service, str(runner.id), "workspace:stopped", data)
    assert received == [(str(task.id), str(runner.id), "/image.qcow2", "/image.qcow2")]
    assert "artifact_id" not in data and "runner_ref" not in data
    task.refresh_from_db()
    workspace.refresh_from_db()
    command = LifecycleCommand.objects.get(task=task)
    assert task.status == "pending"
    assert workspace.current_task_id == task.id
    assert command.phase == "result"
    assert command.heartbeat_at is not None


def test_callback_intervention_restores_workspace_fence(
    runner, workspace, pending_result
) -> None:
    """A callback requiring intervention keeps its identity reserved for recovery."""
    task, data = pending_result

    def callback(task_id: str) -> None:
        TaskRepository.fail(
            Task.objects.get(pk=task_id), "Manual intervention required"
        )

    service = Mock(handle_workspace_stopped=callback)
    assert not apply_result(service, str(runner.id), "workspace:stopped", data)
    workspace.refresh_from_db()
    task.refresh_from_db()
    assert task.status == "failed"
    assert workspace.current_task_id == task.id
    assert workspace.active_operation is None
    assert LifecycleCommand.objects.get(task=task).phase == "intervention"


@pytest.mark.parametrize(
    ("execution_finished", "outcome_known", "accepted"),
    [
        (True, True, True),
        (False, True, False),
        (True, False, False),
        (True, None, False),
    ],
)
def test_late_failure_needs_finished_known_execution(
    runner, workspace, pending_result, execution_finished, outcome_known, accepted
) -> None:
    """A backend timeout can be reconciled only with exact terminal runner proof."""
    task, data = pending_result
    OperationRepository.intervene(task, "Backend deadline; intervention required")
    service = RunnerService(frontend_bus=lambda *args: None)
    assert (
        apply_result(
            service,
            str(runner.id),
            "workspace:error",
            {
                **data,
                "error": "stop failed",
                "observed_status": "running",
                "execution_finished": execution_finished,
                "outcome_known": outcome_known,
            },
        )
        is accepted
    )
    workspace.refresh_from_db()
    task.refresh_from_db()
    assert task.status == "failed"
    assert workspace.current_task_id == (None if accepted else task.id)
    if accepted:
        assert task.error == "stop failed"
