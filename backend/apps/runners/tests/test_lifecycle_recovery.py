"""DB intent, atomic busy fences, terminal replay and worker recovery."""

import uuid
from datetime import timedelta
import pytest
from django.utils import timezone
from common.exceptions import ConflictError
from apps.runners.repositories import TaskRepository
from apps.runners.operations import OperationRepository, apply_result
from apps.runners.models import LifecycleCommand, Task
from apps.runners.enums import TaskType
from apps.runners.tests.test_services import service, sio_mock

pytestmark = pytest.mark.django_db


def allocate(runner, workspace, kind=TaskType.STOP_WORKSPACE):
    return TaskRepository.create(
        task_id=uuid.uuid4(), runner=runner, workspace=workspace, task_type=kind
    )


def test_atomic_busy_conflict(runner, workspace):
    task = allocate(runner, workspace)
    with pytest.raises(ConflictError):
        allocate(runner, workspace, TaskType.RESUME_WORKSPACE)
    workspace.refresh_from_db()
    assert workspace.current_task_id == task.id
    assert Task.objects.filter(workspace=workspace).count() == 1
    assert LifecycleCommand.objects.filter(task=task).exists()


def test_callback_binding_replay_and_credentials_proof(service, runner, workspace):
    workspace.credentials_present = True
    workspace.save()
    task = allocate(runner, workspace)
    row = OperationRepository.prepare(
        str(task.id),
        "task:stop_workspace",
        {"task_id": str(task.id), "workspace_id": str(workspace.id)},
    )
    data = OperationRepository.envelope(
        row, {"task_id": str(task.id), "workspace_id": str(workspace.id)}
    )
    for mismatch in [
        {"attempt": 2},
        {"target": "wrong"},
        {"runner_id": "wrong"},
        {"workspace_id": str(uuid.uuid4())},
    ]:
        assert not apply_result(
            service, str(runner.id), "workspace:stopped", {**data, **mismatch}
        )
    assert not apply_result(service, str(runner.id), "workspace:created", data)
    assert apply_result(service, str(runner.id), "workspace:stopped", data)
    workspace.refresh_from_db()
    assert workspace.credentials_present is True  # stopped is not scrub proof
    assert workspace.current_task_id is None
    new = allocate(runner, workspace, TaskType.RESUME_WORKSPACE)
    assert apply_result(service, str(runner.id), "workspace:stopped", data)
    workspace.refresh_from_db()
    assert workspace.current_task_id == new.id


def test_unprepared_intent_and_lost_lease_bounded(runner, workspace):
    task = allocate(runner, workspace, TaskType.CREATE_WORKSPACE)
    LifecycleCommand.objects.filter(task=task).update(
        created_at=timezone.now() - timedelta(minutes=2)
    )
    OperationRepository.candidates()
    task.refresh_from_db()
    workspace.refresh_from_db()
    assert task.status == "failed"
    assert "intervention" in task.error
    assert workspace.current_task_id == task.id
    assert workspace.active_operation is None


def test_sanitized_outbox_and_offline_expiry(runner, workspace):
    task = allocate(runner, workspace)
    row = OperationRepository.prepare(
        str(task.id),
        "task:stop_workspace",
        {
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
            "env_vars": {"SECRET": "value"},
            "files": [{"content": "secret"}],
        },
    )
    assert "env_vars" not in row.payload and "files" not in row.payload
    runner.sid = "still-connected"
    runner.last_heartbeat_at = timezone.now() - timedelta(minutes=5)
    runner.save()
    assert OperationRepository.candidates() == []
    runner.refresh_from_db()
    workspace.refresh_from_db()
    assert runner.status == "offline"
    assert runner.sid == "still-connected"
    assert workspace.status == "running"
    LifecycleCommand.objects.filter(task=task).update(
        deadline_at=timezone.now() - timedelta(seconds=1)
    )
    OperationRepository.candidates()
    task.refresh_from_db()
    assert task.status == "failed"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_old_session_heartbeat_callback_fenced(runner):
    from unittest.mock import AsyncMock
    from apps.runners.sio_server import _require_runner_id

    transport = AsyncMock()
    transport.get_session.return_value = {"runner_id": str(runner.id)}
    assert await _require_runner_id(transport, runner.sid, "operation:result") == str(
        runner.id
    )
    assert await _require_runner_id(transport, "old-sid", "runner:heartbeat") is None
    assert await _require_runner_id(transport, "old-sid", "operation:result") is None


def test_fresh_execution_heartbeat_suppresses_duplicate_delivery(runner, workspace):
    runner.last_heartbeat_at = timezone.now()
    runner.save()
    task = allocate(runner, workspace)
    row = OperationRepository.prepare(
        str(task.id),
        "task:stop_workspace",
        {"task_id": str(task.id), "workspace_id": str(workspace.id)},
    )
    LifecycleCommand.objects.filter(task=task).update(
        heartbeat_at=timezone.now(), deliveries=12
    )
    assert OperationRepository.candidates() == []
    task.refresh_from_db()
    assert task.status == "pending"
    LifecycleCommand.objects.filter(task=task).update(
        deadline_at=timezone.now() - timedelta(seconds=1)
    )
    OperationRepository.candidates()
    task.refresh_from_db()
    assert task.status == "failed"


def test_late_journal_success_resolves_backend_deadline(service, runner, workspace):
    task = allocate(runner, workspace)
    row = OperationRepository.prepare(
        str(task.id),
        "task:stop_workspace",
        {"task_id": str(task.id), "workspace_id": str(workspace.id)},
    )
    OperationRepository.intervene(task, "Backend deadline; outcome unknown")
    data = OperationRepository.envelope(
        row,
        {
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
            "credentials_present": False,
        },
    )
    assert apply_result(service, str(runner.id), "workspace:stopped", data)
    workspace.refresh_from_db()
    task.refresh_from_db()
    assert task.status == "completed"
    assert workspace.current_task_id is None
    assert workspace.credentials_present is False


@pytest.mark.parametrize(
    ("initial", "acknowledged"),
    [(True, False), (False, True), (True, None), (False, None)],
)
def test_resume_envelope_applies_exact_credential_acknowledgement(
    service, runner, workspace, monkeypatch, initial, acknowledged
):
    """Completion and frontend state agree immediately, before any heartbeat."""
    workspace.credentials_present = initial
    workspace.status = "stopped"
    workspace.save()
    task = allocate(runner, workspace, TaskType.RESUME_WORKSPACE)
    row = OperationRepository.prepare(
        str(task.id),
        "task:resume_workspace",
        {"task_id": str(task.id), "workspace_id": str(workspace.id)},
    )
    data = OperationRepository.envelope(
        row,
        {
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
            "credentials_present": acknowledged,
        },
    )
    forwarded = []
    monkeypatch.setattr(
        service, "_forward_to_frontend", lambda *args: forwarded.append(args)
    )
    for mismatch in [
        {"task_id": str(uuid.uuid4())},
        {"workspace_id": str(uuid.uuid4())},
        {"attempt": row.attempt + 1},
        {"runner_id": str(uuid.uuid4())},
    ]:
        assert not apply_result(
            service, str(runner.id), "workspace:resumed", {**data, **mismatch}
        )
        workspace.refresh_from_db()
        assert workspace.credentials_present is initial
        assert workspace.current_task_id == task.id
    assert not forwarded
    assert apply_result(service, str(runner.id), "workspace:resumed", data)
    workspace.refresh_from_db()
    task.refresh_from_db()
    expected = initial if acknowledged is None else acknowledged
    assert workspace.status == "running"
    assert workspace.credentials_present is expected
    assert workspace.current_task_id is None
    assert workspace.active_operation is None
    assert task.status == "completed"
    payload = next(
        args[1] for args in forwarded if args[0] == "workspace:status_changed"
    )
    assert payload["credentials_present"] is expected
    new = allocate(runner, workspace, TaskType.STOP_WORKSPACE)
    count = len(forwarded)
    # A late terminal replay is ACKable, but cannot rewrite state or the new fence.
    assert apply_result(
        service,
        str(runner.id),
        "workspace:resumed",
        {**data, "credentials_present": not expected},
    )
    workspace.refresh_from_db()
    assert workspace.credentials_present is expected
    assert workspace.current_task_id == new.id
    assert len(forwarded) == count
