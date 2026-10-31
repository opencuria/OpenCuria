"""Only exact permanent recovery evidence needs repair; late proof still closes it."""

from __future__ import annotations

import uuid
from unittest.mock import Mock, patch

import pytest

from apps.runners.deletion_repository import DeletionRepository
from apps.runners.disposition_repository import DispositionRepository
from apps.runners.models import CaptureRequest, ImageDeletionRequest, LifecycleCommand
from apps.runners.operations import OperationRepository
from apps.runners.services.capture import CaptureService
from apps.runners.services.disposition import DispositionService
from apps.runners.services.operation_results import apply_result
from apps.runners.tests.test_runner_activity import detail
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock
from apps.runners.workspace_operation_repository import WorkspaceOperationRepository

pytestmark = pytest.mark.django_db


def blocked_capture(workspace):
    """Create the real stopped-source capture and an exact permanent observation."""
    workspace.status = "stopped"
    workspace.credentials_present = False
    workspace.save(update_fields=["status", "credentials_present"])
    _, task = CaptureService.allocate(
        workspace.id, "Recovery attention", has_busy_agent=lambda _: False
    )
    command = LifecycleCommand.objects.get(task=task)
    return (
        task,
        command,
        {
            "identity": OperationRepository.envelope(command, {}),
            "instance_id": "current-exclusive-runner",
            "status": "unknown",
            "execution_finished": False,
            "quiescent": False,
            "recovery_blocked_code": "publication_ownership_conflict",
            "diagnostic": "Publication staging inode differs from its ownership record",
        },
    )


@pytest.mark.parametrize("field", ["operation_id", "attempt", "target", "runner_id"])
def test_blocked_evidence_requires_exact_identity(service, runner, workspace, field):
    task, command, proof = blocked_capture(workspace)
    proof["identity"][field] = 999 if field == "attempt" else str(uuid.uuid4())
    assert not DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )
    command.refresh_from_db()
    task.refresh_from_db()
    assert command.phase == "delivery" and task.status == "pending"


@pytest.mark.parametrize("changed", ["wrong_session", "offline", "released_workspace"])
def test_blocked_evidence_rechecks_session_and_reservation(
    service, runner, workspace, changed
):
    task, command, proof = blocked_capture(workspace)
    sid = runner.sid
    if changed == "wrong_session":
        sid = "obsolete-session"
    elif changed == "offline":
        runner.status = "offline"
        runner.save(update_fields=["status"])
    else:
        workspace.current_task = None
        workspace.save(update_fields=["current_task"])
    assert not DispositionService.recover_inspection(
        service, str(runner.id), sid, proof
    )
    command.refresh_from_db()
    assert command.phase == "delivery"


@pytest.mark.parametrize(
    "invalid",
    [
        {"recovery_blocked_code": "timeout"},
        {"recovery_blocked_code": {}},
        {"diagnostic": ""},
        {"diagnostic": {}},
        {"instance_id": ""},
        {"status": "running"},
        {"quiescent": True},
        {"execution_finished": True},
    ],
)
def test_uncertainty_or_malformed_failure_cannot_request_attention(
    service, runner, workspace, invalid
):
    task, command, proof = blocked_capture(workspace)
    assert not DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, {**proof, **invalid}
    )
    command.refresh_from_db()
    assert command.phase == "delivery"


def test_permanent_blocker_retains_fence_and_survives_coordinator_ticks(
    service, runner, workspace, user
):
    task, command, proof = blocked_capture(workspace)
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )
    task.refresh_from_db()
    expected_error = task.error
    for _ in range(2):
        CaptureService.tick()
        command.refresh_from_db()
        task.refresh_from_db()
        workspace.refresh_from_db()
        assert command.phase == "attention_required"
        assert task.status == "failed" and task.error == expected_error
        assert workspace.current_task_id == task.id
        assert WorkspaceOperationRepository.project(workspace.id, None) is None
    assert task.id in {row.task_id for row in OperationRepository.list_for_inspection()}
    activity = detail(user, runner)["activities"][0]
    assert activity["kind"] == "capture"
    assert activity["state"] == "attention" and activity["outcome"] == "blocked"
    assert proof["diagnostic"] in activity["diagnostic"]
    assert "administrator repair" in activity["summary"]
    assert DispositionService._summary(
        DispositionRepository.for_runner(str(runner.id), str(task.id))
    )["intervention_required"]


def test_repeated_unknown_result_cannot_downgrade_or_spam_repair_blocker(
    service, runner, workspace
):
    task, command, proof = blocked_capture(workspace)
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )
    task.refresh_from_db()
    error = task.error
    with patch.object(service, "_forward_to_frontend", Mock()) as frontend:
        assert not DispositionService.recover_inspection(
            service, str(runner.id), runner.sid, proof
        )
        assert not apply_result(
            service,
            str(runner.id),
            "image_artifact:failed",
            {
                **proof["identity"],
                "task_id": str(task.id),
                "workspace_id": str(workspace.id),
                "outcome_known": False,
                "execution_finished": True,
            },
        )
        frontend.assert_not_called()
    command.refresh_from_db()
    task.refresh_from_db()
    assert command.phase == "attention_required" and task.error == error


def test_late_terminal_inspection_automatically_closes_repair_blocker(
    service, runner, workspace
):
    task, command, proof = blocked_capture(workspace)
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )
    terminal = {
        key: value
        for key, value in proof.items()
        if key not in {"recovery_blocked_code", "diagnostic"}
    }
    terminal.update(
        status="terminal",
        execution_finished=True,
        quiescent=True,
        outcome_known=False,
        workspace_status="stopped",
    )
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, terminal
    )
    command.refresh_from_db()
    workspace.refresh_from_db()
    assert command.phase == "disposed"
    assert workspace.current_task_id is None and workspace.active_operation is None
    assert CaptureRequest.objects.get(child=task).phase == "failed"
    assert task.id not in {
        row.task_id for row in OperationRepository.list_for_inspection()
    }
    # Delayed contradictory observations cannot revive the resolved identity.
    assert not DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )
    command.refresh_from_db()
    assert command.phase == "disposed"


def test_late_known_result_reconciles_after_explicit_repair_blocker(
    service, runner, workspace
):
    task, command, proof = blocked_capture(workspace)
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )
    assert apply_result(
        service,
        str(runner.id),
        "image_artifact:created",
        {
            **proof["identity"],
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
            "image_artifact_id": str(uuid.uuid4()),
            "name": "Recovered publication",
            "size_bytes": 100,
            "outcome_known": True,
            "execution_finished": True,
        },
    )
    task.refresh_from_db()
    command.refresh_from_db()
    workspace.refresh_from_db()
    assert task.status == "completed" and command.phase == "result"
    assert workspace.current_task_id is None


def test_deletion_parent_keeps_explicit_child_repair_phase(
    service, runner, workspace, user
):
    task, command, proof = blocked_capture(workspace)
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )
    task.refresh_from_db()
    request = ImageDeletionRequest.objects.create(
        organization=runner.organization,
        requested_by=user,
        target_type="image",
        target_id=CaptureRequest.objects.get(child=task).image_id,
        phase="executing",
        children={"image:fixture": str(task.id)},
        approval={},
    )
    with patch.object(DeletionRepository, "graph", return_value={}):
        DeletionRepository.advance(request.id)
    request.refresh_from_db()
    assert request.phase == "attention_required" and request.diagnostic == task.error
