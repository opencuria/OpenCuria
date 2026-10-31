"""Exercise combinations planned in docs/testing/lifecycle-backend-cases.md."""

from __future__ import annotations

import uuid
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.apps import apps
from django.db import connection
from django.utils import timezone

from apps.runners.models import (
    CaptureRequest,
    ImageInstance,
    LifecycleCommand,
    Task,
    Workspace,
)
from apps.runners.operations import LIFECYCLE_TYPES, OperationRepository
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService
from apps.runners.services.disposition import DispositionService
from apps.runners.tests.conftest import has_busy_agent
from apps.runners.tests.test_capture_disposition import interrupted_capture
from apps.runners.tests.test_image_generations import definition as definition
from apps.runners.tests.test_image_generations import request
from apps.runners.tests.test_image_versions import make_line, qemu_workspace
from apps.runners.tests.test_recreate_workflow import RecreateService
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db
KINDS = sorted(LIFECYCLE_TYPES)
WORKSPACE_KINDS = [
    kind for kind in KINDS if kind not in {"build_image", "delete_image"}
]


def operation(kind, runner, workspace, user, definition):
    """Create realistic correlated task/image records for each lifecycle kind."""
    image = job = None
    if kind == "build_image":
        job, image, task = request(definition, runner, user)
    else:
        task = TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=runner,
            workspace=None if kind == "delete_image" else workspace,
            task_type=kind,
            operation_payload={"workspace_id": str(workspace.id)},
        )
        if kind in {"create_image_artifact", "delete_image"}:
            image = ImageInstance.objects.create(
                runner=runner,
                name="Matrix image",
                status="capturing" if kind == "create_image_artifact" else "deleting",
                is_legacy=True,
                creating_task=task if kind == "create_image_artifact" else None,
                deleting_task=task if kind == "delete_image" else None,
                origin_workspace=workspace if kind == "create_image_artifact" else None,
                runner_ref="/matrix.qcow2",
            )
    command = LifecycleCommand.objects.get(task=task)
    return task, command, image, job


def evidence(command, observed="stopped"):
    """Build fresh authenticated proof of a sealed, quiescent interruption."""
    return {
        "identity": OperationRepository.envelope(command, {}),
        "instance_id": "exclusive-current-process",
        "status": "terminal",
        "execution_finished": True,
        "quiescent": True,
        "outcome_known": False,
        "workspace_status": observed,
    }


@pytest.mark.parametrize(
    "observed", ["running", "stopped", "absent", "unknown", "exited"]
)
@pytest.mark.parametrize("kind", KINDS)
def test_interrupted_operation_matrix(
    runner, workspace, user, definition, kind, observed
):
    task, command, image, job = operation(kind, runner, workspace, user, definition)
    original = (
        workspace.base_image_instance_id,
        workspace.pending_base_image_instance_id,
    )
    count = Task.objects.count()
    service = RunnerService(frontend_bus=Mock())
    proof = evidence(command, observed)
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )
    task.refresh_from_db()
    command.refresh_from_db()
    workspace.refresh_from_db()
    assert task.status == "failed" and command.phase == "disposed"
    assert "interrupted" in task.error and "admin" not in task.error.lower()
    assert Task.objects.count() == count  # never execute scripts under a new identity
    assert workspace.current_task_id is None and workspace.active_operation is None
    assert (
        workspace.base_image_instance_id,
        workspace.pending_base_image_instance_id,
    ) == original
    if kind in WORKSPACE_KINDS:
        expected = {
            "running": "running",
            "stopped": "stopped",
            "exited": "stopped",
        }.get(observed, "failed")
        if kind.startswith("create_workspace"):
            expected = (
                "failed"  # arbitrary initialization cannot be inferred from power state
            )
        if kind == "remove_workspace":
            expected = "delete_failed"
        assert workspace.status == expected
    if image:
        image.refresh_from_db()
        assert image.status == ("delete_failed" if kind == "delete_image" else "failed")
    if job:
        job.refresh_from_db()
        assert job.status == "failed" and job.pending_generation_id is None
    # Duplicate inspection is harmless, and the failed execution remains history.
    assert not DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, proof
    )


@pytest.mark.parametrize(
    "bad",
    [
        "runner",
        "operation_id",
        "attempt",
        "target",
        "instance",
        "finished",
        "quiescent",
        "running",
        "unsealed",
        "known_unfinished",
        "missing_outcome",
        "replaced_session",
    ],
)
def test_unsafe_inspection_matrix(runner, workspace, user, definition, bad):
    task, command, _, _ = operation(
        "stop_workspace", runner, workspace, user, definition
    )
    proof = evidence(command)
    runner_id, sid = str(runner.id), runner.sid
    if bad == "runner":
        runner_id = str(uuid.uuid4())
    elif bad in {"operation_id", "attempt", "target"}:
        proof["identity"][bad] = "mismatch"
    elif bad == "instance":
        proof["instance_id"] = ""
    elif bad == "finished":
        proof["execution_finished"] = False
    elif bad == "quiescent":
        proof["quiescent"] = False
    elif bad in {"running", "unsealed"}:
        proof["status"] = "running" if bad == "running" else "unknown"
    elif bad == "known_unfinished":
        proof.update(outcome_known=True, execution_finished=False)
    elif bad == "missing_outcome":
        proof.pop("outcome_known")
    elif bad == "replaced_session":
        sid = "old-session"
    assert not DispositionService.recover_inspection(Mock(), runner_id, sid, proof)
    task.refresh_from_db()
    workspace.refresh_from_db()
    assert task.status == "pending" and workspace.current_task_id == task.id


@pytest.mark.parametrize("kind", KINDS)
def test_known_inspection_dispatch(runner, workspace, user, definition, kind):
    _, command, _, _ = operation(kind, runner, workspace, user, definition)
    proof = {
        **evidence(command),
        "outcome_known": True,
        "event": "exact:event",
        "result": {"exact": "payload"},
    }
    service = Mock()
    with patch(
        "apps.runners.services.disposition.apply_result", return_value=True
    ) as apply:
        assert DispositionService.recover_inspection(
            service, str(runner.id), runner.sid, proof
        )
    apply.assert_called_once_with(
        service, str(runner.id), "exact:event", {"exact": "payload"}
    )


@pytest.mark.parametrize("kind", KINDS)
def test_unprepared_intent_matrix(runner, workspace, kind):
    task = Task.objects.create(
        runner=runner,
        workspace=workspace if kind in WORKSPACE_KINDS else None,
        type=kind,
        status="pending",
    )
    OperationRepository.allocate(task)
    LifecycleCommand.objects.filter(task=task).update(
        created_at=timezone.now() - timedelta(minutes=2)
    )
    assert OperationRepository.candidates(delivery_allowed=lambda _: True) == []
    task.refresh_from_db()
    command = LifecycleCommand.objects.get(task=task)
    workspace.refresh_from_db()
    assert task.status == "failed" and command.phase == "disposed"
    assert workspace.current_task_id is None
    assert not workspace.intervention_required
    with pytest.raises(ConflictError, match="already ended"):
        OperationRepository.prepare(str(task.id), f"task:{kind}", {})


@pytest.mark.parametrize("kind", KINDS)
def test_live_heartbeat_matrix(runner, workspace, user, definition, kind):
    task, command, _, _ = operation(kind, runner, workspace, user, definition)
    now = timezone.now()
    LifecycleCommand.objects.filter(task=task).update(
        deadline_at=now - timedelta(hours=4), deliveries=12
    )
    data = {**OperationRepository.envelope(command, {}), "task_id": str(task.id)}
    OperationRepository.record_heartbeat(str(runner.id), data)
    assert OperationRepository.candidates(delivery_allowed=lambda _: True) == []
    command.refresh_from_db()
    task.refresh_from_db()
    assert command.deadline_at > now and command.phase == "executing"
    assert task.status == "pending" and command.deliveries == 12


@pytest.mark.parametrize("kind", KINDS)
def test_stale_heartbeat_matrix(runner, workspace, user, definition, kind):
    task, command, _, _ = operation(kind, runner, workspace, user, definition)
    LifecycleCommand.objects.filter(task=task).update(
        deadline_at=timezone.now() - timedelta(seconds=1),
        deliveries=1,
        heartbeat_at=timezone.now() - timedelta(minutes=5),
    )
    assert OperationRepository.candidates(delivery_allowed=lambda _: True) == []
    task.refresh_from_db()
    command.refresh_from_db()
    workspace.refresh_from_db()
    assert task.status == "failed" and command.phase == "intervention"
    if kind in WORKSPACE_KINDS:
        assert workspace.current_task_id == task.id
    assert command.deliveries == 1


@pytest.mark.parametrize(
    "kind",
    [
        "stop_workspace",
        "resume_workspace",
        "update_workspace",
        "remove_workspace",
        "create_workspace",
        "create_image_artifact",
    ],
)
def test_newer_owner_matrix(runner, workspace, user, definition, kind):
    task, command, _, _ = operation(kind, runner, workspace, user, definition)
    newer = Task.objects.create(
        runner=runner, workspace=workspace, type="stop_workspace"
    )
    Workspace.objects.filter(pk=workspace.id).update(current_task=newer)
    assert not DispositionService.recover_inspection(
        Mock(), str(runner.id), runner.sid, evidence(command)
    )
    workspace.refresh_from_db()
    task.refresh_from_db()
    assert workspace.current_task_id == newer.id and task.status == "pending"


@pytest.mark.parametrize(
    "mode",
    [
        "capture-stop",
        "capture-image",
        "capture-resume",
        "recreate-remove",
        "recreate-create",
        "recreate-stop",
    ],
)
def test_parent_interruption_matrix(service, runner, workspace, user, mode):
    if mode.startswith("capture"):
        phase = {
            "capture-stop": "stop",
            "capture-image": "capture",
            "capture-resume": "resume",
        }[mode]
        parent, command, _ = interrupted_capture(service, runner, workspace, phase)
    else:
        _, (image,) = make_line(runner, user)
        workspace = qemu_workspace(
            runner,
            user,
            image,
            status="stopped" if mode == "recreate-stop" else "running",
        )
        _, task, parent = RecreateService.allocate(
            workspace.id, image.id, has_busy_agent=has_busy_agent
        )
        if mode in {"recreate-create", "recreate-stop"}:
            from apps.runners.tests.test_capture_pipeline import result

            assert result(service, runner, task, "workspace:removed", result="deleted")
            RecreateService.tick()
            parent.refresh_from_db()
            if mode == "recreate-stop":
                assert result(
                    service, runner, parent.child, "workspace:created", status="running"
                )
                RecreateService.tick()
                parent.refresh_from_db()
                assert parent.phase == "stop"
        command = LifecycleCommand.objects.get(task=parent.child)
    count = Task.objects.count()
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, evidence(command)
    )
    parent.refresh_from_db()
    workspace.refresh_from_db()
    assert parent.phase == "failed" and parent.diagnostic
    assert workspace.current_task_id is None and workspace.active_operation is None
    assert Task.objects.count() == count
    if isinstance(parent, CaptureRequest):
        parent.image.refresh_from_db()
        assert parent.image.status == (
            "ready" if mode == "capture-resume" else "failed"
        )
        assert parent.resume_suppressed


@pytest.mark.parametrize("proof", [None, False, True])
@pytest.mark.parametrize("initial", [False, True])
def test_credential_proof_matrix(runner, workspace, user, definition, initial, proof):
    workspace.credentials_present = initial
    workspace.save(update_fields=["credentials_present"])
    _, command, _, _ = operation("stop_workspace", runner, workspace, user, definition)
    packet = {**evidence(command), "credentials_present": proof}
    assert DispositionService.recover_inspection(
        RunnerService(frontend_bus=Mock()), str(runner.id), runner.sid, packet
    )
    workspace.refresh_from_db()
    assert workspace.credentials_present is (initial if proof is None else proof)


def test_user_diagnostic(runner, workspace, user, definition):
    task, command, _, _ = operation(
        "create_workspace", runner, workspace, user, definition
    )
    assert DispositionService.recover_inspection(
        RunnerService(frontend_bus=Mock()),
        str(runner.id),
        runner.sid,
        evidence(command),
    )
    task.refresh_from_db()
    workspace.refresh_from_db()
    assert task.status == "failed" and not workspace.intervention_required
    assert "Retry" in task.error and "admin" not in task.error


def test_never_delivered_offline_stays_queued(runner, workspace, user, definition):
    task, command, _, _ = operation(
        "stop_workspace", runner, workspace, user, definition
    )
    runner.status, runner.sid = "offline", ""
    runner.save(update_fields=["status", "sid"])
    LifecycleCommand.objects.filter(task=task).update(
        deadline_at=timezone.now() - timedelta(days=3)
    )
    assert OperationRepository.candidates(delivery_allowed=lambda _: True) == []
    task.refresh_from_db()
    command.refresh_from_db()
    assert task.status == "pending" and command.deliveries == 0
    runner.status, runner.sid = "online", "new-session"
    runner.save(update_fields=["status", "sid"])
    deliveries = OperationRepository.candidates(delivery_allowed=lambda _: True)
    assert len(deliveries) == 1 and deliveries[0].task_id == task.id


def test_unreproducible_input_fails_without_fence(runner, workspace):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="create_workspace",
    )
    command = OperationRepository.prepare(
        str(task.id),
        "task:create_workspace",
        {"env_vars": {"PRIVATE": "never logged"}, "workspace_id": str(workspace.id)},
    )
    with pytest.raises(ConflictError, match="Unreproducible"):
        OperationRepository.delivery_payload(command)
    task.refresh_from_db()
    workspace.refresh_from_db()
    command.refresh_from_db()
    assert task.status == "failed" and command.phase == "disposed"
    assert workspace.current_task_id is None and "never logged" not in task.error


def test_removed_migration_normalizes_only_terminal_label(runner, workspace):
    Workspace.objects.filter(pk=workspace.id).update(
        status="removed", credentials_present=True
    )
    migration = import_module(
        "apps.runners.migrations.0031_normalize_workspace_deleted"
    )
    migration.normalize_removed(apps, SimpleNamespace(connection=connection))
    migration.normalize_removed(apps, SimpleNamespace(connection=connection))
    workspace.refresh_from_db()
    assert workspace.status == "deleted" and workspace.credentials_present is True


def test_direct_delivery_boundary(runner, workspace):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    assert OperationRepository.claim_direct_delivery(task.id) == runner.sid
    command = LifecycleCommand.objects.get(task=task)
    assert command.deliveries == 1 and command.lease_until > timezone.now()
    assert OperationRepository.claim_direct_delivery(task.id) is None
    assert OperationRepository.candidates(delivery_allowed=lambda _: True) == []
    OperationRepository.fail_unissued(task, "Payload unavailable after issue")
    workspace.refresh_from_db()
    command.refresh_from_db()
    assert command.phase == "intervention" and workspace.current_task_id == task.id


def test_direct_delivery_uses_current_session(runner, workspace):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    type(runner).objects.filter(pk=runner.id).update(sid="newer-session")
    assert OperationRepository.claim_direct_delivery(task.id) == "newer-session"
    TaskRepository.complete(task)
    assert OperationRepository.claim_direct_delivery(task.id) is None


def test_sealed_deletion_retry(runner, user):
    from apps.runners.deletion_repository import DeletionRepository as Repo
    from apps.runners.models import ImageDeletionRequest
    from apps.runners.tests.test_image_deletion_coordinator import (
        image,
        observe,
        resource,
    )
    from apps.runners.tests.test_image_deletion_coordinator import (
        request as delete_request,
    )

    base = image(runner, user)
    observe(runner, [resource(base)])
    plan = delete_request(runner, user, base)
    Repo.tick()
    parent = ImageDeletionRequest.objects.get(pk=plan["id"])
    first = next(iter(parent.children.values()))
    command = LifecycleCommand.objects.get(task_id=first)
    OperationRepository.intervene(command.task, "Runner interrupted during deletion")
    assert DispositionService.recover_inspection(
        RunnerService(frontend_bus=Mock()),
        str(runner.id),
        runner.sid,
        evidence(command),
    )
    Repo.tick()
    parent.refresh_from_db()
    assert parent.phase == "waiting_inventory"
    observe(runner, [resource(base)])
    Repo.tick()
    parent.refresh_from_db()
    assert next(iter(parent.children.values())) != first
    assert parent.approval["retry_counts"]["image:" + str(base.id)] == 1


def test_exhausted_deletion_retry(runner, user):
    from apps.runners.deletion_repository import DeletionRepository as Repo
    from apps.runners.models import ImageDeletionRequest
    from apps.runners.tests.test_image_deletion_coordinator import (
        image,
        observe,
        resource,
    )
    from apps.runners.tests.test_image_deletion_coordinator import (
        request as delete_request,
    )

    base = image(runner, user)
    observe(runner, [resource(base)])
    plan = delete_request(runner, user, base)
    service = RunnerService(frontend_bus=Mock())
    for attempt in range(3):
        Repo.tick()
        parent = ImageDeletionRequest.objects.get(pk=plan["id"])
        command = LifecycleCommand.objects.get(
            task_id=next(iter(parent.children.values()))
        )
        assert DispositionService.recover_inspection(
            service, str(runner.id), runner.sid, evidence(command)
        )
        Repo.tick()
        observe(runner, [resource(base)])
    parent.refresh_from_db()
    assert parent.phase == "failed" and "Retry deletion" in parent.diagnostic
    prior_approval = parent.approval["images"]
    result = delete_request(runner, user, base)
    assert result["id"] == str(parent.id) and result["phase"] == "waiting_inventory"
    parent.refresh_from_db()
    assert parent.approval["images"] == prior_approval and not parent.children
    Repo.tick()
    parent.refresh_from_db()
    assert parent.children and Task.objects.count() == 4
