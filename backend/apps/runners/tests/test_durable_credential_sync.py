"""Credential writes use the same durable ownership as capture and lifecycle."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from apps.credentials.services import ResolvedCredentials
from apps.runners.models import LifecycleCommand, Runner, Task
from apps.runners.operations import OperationRepository
from apps.runners.services import RunnerService
from apps.runners.services.capture import CaptureService
from apps.runners.services.disposition import DispositionService
from apps.runners.services.recovery import RecoveryService
from apps.runners.tests.conftest import has_busy_agent
from apps.runners.tests.test_services import (
    TestPersistentWorkspaceCredentials as PersistentCredentials,
)
from apps.runners.tests.test_services import (
    service as service,
)
from apps.runners.tests.test_services import (
    sio_mock as sio_mock,
)
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.asyncio
async def test_lost_credential_ack_keeps_durable_fence_then_recovers(
    runner, workspace, user, service, sio_mock
):
    credential = PersistentCredentials._org_env_credential(
        runner, user, name="Durable token", env_var="TOKEN", value="SECRET_WRITE"
    )
    workspace.credentials.add(credential)
    sio_mock.call = AsyncMock(side_effect=SocketIOTimeoutError())
    with pytest.raises(ConflictError):
        await service._dispatch_credential_inject(workspace, wait=True)
    task = Task.objects.get(type="inject_credentials", workspace=workspace)
    command = LifecycleCommand.objects.get(task=task)
    workspace.refresh_from_db()
    assert task.status == "in_progress"
    assert workspace.current_task_id == task.id
    assert command.deliveries == 1
    assert "SECRET_WRITE" not in json.dumps(command.payload)
    with pytest.raises(ConflictError):
        CaptureService.allocate(workspace.id, "blocked", has_busy_agent=has_busy_agent)
    # A fresh backend process retains delivery and can resolve a replayed result.
    restarted = RunnerService(sio_server=sio_mock)
    command.lease_until = command.next_delivery_at = command.created_at
    command.save()
    await RecoveryService().tick(sio_mock)
    delivery = next(
        call.args[1]
        for call in sio_mock.emit.await_args_list
        if call.args[0] == "task:inject_credentials"
    )
    assert delivery["env_vars"] == {"TOKEN": "SECRET_WRITE"}
    assert delivery["operation_id"] == str(task.id)
    accepted = restarted.apply_operation_result(
        str(runner.id),
        "workspace:credentials_injected",
        {**delivery, "credentials_present": True, "execution_finished": True},
    )
    assert accepted
    workspace.refresh_from_db()
    task.refresh_from_db()
    assert task.status == "completed" and workspace.current_task_id is None
    assert workspace.credentials_present


@pytest.mark.parametrize("proof", [None, False, True])
def test_interrupted_injection_requires_fresh_scrub_proof(runner, workspace, proof):
    from apps.runners.repositories import TaskRepository

    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="inject_credentials",
        operation_payload={},
    )
    command = LifecycleCommand.objects.get(task=task)
    OperationRepository.intervene(task, "Interrupted credential write")
    evidence = {
        "identity": OperationRepository.envelope(command, {}),
        "instance_id": "current-process",
        "status": "terminal",
        "execution_finished": True,
        "quiescent": True,
        "outcome_known": False,
        "workspace_status": "stopped",
    }
    if proof is not None:
        evidence["credentials_present"] = proof
    assert DispositionService.recover_inspection(
        RunnerService(), str(runner.id), runner.sid, evidence
    )
    workspace.refresh_from_db()
    assert workspace.current_task_id is None
    assert workspace.credentials_present == (proof is not False)


@pytest.mark.parametrize("wrong", ["workspace", "runner", "late", "type", "flag"])
def test_credential_callback_cannot_mutate_unowned_or_terminal_work(
    runner, workspace, wrong
):
    from apps.runners.repositories import TaskRepository

    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="inject_credentials",
    )
    workspace_id, runner_id, present = str(workspace.id), str(runner.id), True
    if wrong == "workspace":
        workspace_id = str(uuid.uuid4())
    elif wrong == "runner":
        runner_id = str(uuid.uuid4())
    elif wrong == "late":
        Task.objects.filter(pk=task.id).update(status="failed")
    elif wrong == "type":
        Task.objects.filter(pk=task.id).update(type="resume_workspace")
    else:
        present = None
    RunnerService().handle_credentials_injected(
        str(task.id), workspace_id, present, runner_id=runner_id
    )
    workspace.refresh_from_db()
    assert workspace.credentials_present is False
    assert workspace.current_task_id == task.id


@pytest.mark.asyncio
async def test_old_session_ack_cannot_complete_current_injection(
    runner, workspace, service, sio_mock
):
    async def reconnect(*args, **kwargs):
        Runner.objects.filter(pk=runner.id).update(sid="replacement-session")
        return {"ok": True, "credentials_present": True}

    sio_mock.call = AsyncMock(side_effect=reconnect)
    assert (
        await service._dispatch_credential_inject(
            workspace, resolved=ResolvedCredentials(), wait=True
        )
        is None
    )
    task = Task.objects.get(type="inject_credentials", workspace=workspace)
    assert task.status == "in_progress"
    workspace.refresh_from_db()
    assert workspace.current_task_id == task.id
    assert workspace.credentials_present is False


def test_legacy_credential_sync_waits_for_upgraded_runner_and_exact_quiescence(
    runner, workspace
):
    from apps.runners.credential_sync_repository import LegacyCredentialSyncRepository

    task = Task.objects.create(
        runner=runner,
        workspace=workspace,
        type="inject_credentials",
        status="in_progress",
    )
    LegacyCredentialSyncRepository.fence()
    command = LifecycleCommand.objects.get(task=task)
    workspace.refresh_from_db()
    task.refresh_from_db()
    assert command.phase == "intervention" and task.status == "failed"
    assert command.payload["_legacy_credential_sync"] is True
    assert workspace.current_task_id == task.id
    assert OperationRepository.candidates(delivery_allowed=lambda task_id: True) == []
    evidence = {
        "identity": OperationRepository.envelope(command, {}),
        "instance_id": "process",
        "status": "terminal",
        "execution_finished": True,
        "quiescent": True,
        "outcome_known": False,
        "workspace_status": "stopped",
    }
    # The old runner also has operation inspection, so a generic terminal seal
    # alone cannot prove that delayed legacy credential packets are rejected.
    service = RunnerService()
    assert not DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, evidence
    )
    evidence["durable_credentials"] = True
    assert not DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, evidence
    )
    evidence["legacy_credential_quiesced"] = True
    assert DispositionService.recover_inspection(
        service, str(runner.id), runner.sid, evidence
    )
    workspace.refresh_from_db()
    command.refresh_from_db()
    assert workspace.current_task_id is None and command.phase == "disposed"
    assert workspace.credentials_present is True
    assert workspace.status == "stopped"


def test_duplicate_interruption_does_not_repeat_notifications(
    runner, workspace, service
):
    from unittest.mock import Mock

    from apps.runners.repositories import TaskRepository

    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="inject_credentials",
        operation_payload={},
    )
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(
        command,
        {
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
            "error": "uncertain",
            "outcome_known": False,
        },
    )
    service._forward_to_frontend = Mock()
    service._forward_workspace_operation = Mock()
    assert not service.apply_operation_result(str(runner.id), "workspace:error", data)
    assert not service.apply_operation_result(str(runner.id), "workspace:error", data)
    service._forward_to_frontend.assert_called_once()
    service._forward_workspace_operation.assert_called_once()


def test_legacy_credential_history_does_not_resurrect_deleted_workspace(
    runner, workspace
):
    from apps.runners.credential_sync_repository import LegacyCredentialSyncRepository
    from apps.runners.models import Workspace

    Workspace.objects.filter(pk=workspace.id).update(status="deleted")
    task = Task.objects.create(
        runner=runner,
        workspace=workspace,
        type="inject_credentials",
        status="in_progress",
    )
    LegacyCredentialSyncRepository.fence()
    command = LifecycleCommand.objects.get(task=task)
    assert DispositionService.recover_inspection(
        RunnerService(),
        str(runner.id),
        runner.sid,
        {
            "identity": OperationRepository.envelope(command, {}),
            "instance_id": "new-runner",
            "status": "terminal",
            "execution_finished": True,
            "quiescent": True,
            "outcome_known": False,
            "workspace_status": "absent",
            "durable_credentials": True,
            "legacy_credential_quiesced": True,
        },
    )
    workspace.refresh_from_db()
    assert workspace.status == "deleted" and workspace.current_task_id is None


def test_new_credential_write_waits_for_legacy_fence_before_admission(
    runner, workspace
):
    from apps.runners.credential_sync_repository import LegacyCredentialSyncRepository
    from apps.runners.repositories import TaskRepository

    legacy = Task.objects.create(
        runner=runner,
        workspace=workspace,
        type="inject_credentials",
        status="in_progress",
    )
    with pytest.raises(ConflictError, match="legacy credential"):
        TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=runner,
            workspace=workspace,
            task_type="inject_credentials",
        )
    assert Task.objects.filter(workspace=workspace).count() == 1
    LegacyCredentialSyncRepository.fence()
    command = LifecycleCommand.objects.get(task=legacy)
    with pytest.raises(ConflictError, match="unresolved lifecycle"):
        TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=runner,
            workspace=workspace,
            task_type="inject_credentials",
        )
    assert DispositionService.recover_inspection(
        RunnerService(),
        str(runner.id),
        runner.sid,
        {
            "identity": OperationRepository.envelope(command, {}),
            "instance_id": "new-runner",
            "status": "terminal",
            "execution_finished": True,
            "quiescent": True,
            "outcome_known": False,
            "workspace_status": "stopped",
            "durable_credentials": True,
            "legacy_credential_quiesced": True,
        },
    )
    admitted = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="inject_credentials",
    )
    workspace.refresh_from_db()
    assert workspace.current_task_id == admitted.id
