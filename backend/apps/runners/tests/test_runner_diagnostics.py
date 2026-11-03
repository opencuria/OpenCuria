"""Runner diagnostic codes reach users as actionable text and outlive the fence."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import APIKeyPermission
from apps.organizations.models import Membership
from apps.runners.models import LifecycleCommand, Task, Workspace
from apps.runners.operations import OperationRepository
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService
from apps.runners.services.disposition import DispositionService
from apps.runners.services.operation_results import apply_result
from apps.runners.services.runner_diagnostics import (
    FAILED,
    INTERRUPTED,
    RECOVERED,
    failure_message,
    recovered_message,
)
from apps.runners.tests.test_workspace_access_controls import _make_client

pytestmark = pytest.mark.django_db

SHRINK = "The disk cannot be made smaller. Keep at least the current disk size."
STORAGE = "The runner is out of disk space."
STORAGE_ACTION = "Free up storage on the runner, then try again."
IMAGE = "The image is missing on the runner. Rebuild the image or choose another one."


def lifecycle(runner, workspace, kind: str) -> tuple[Task, dict]:
    """Allocate a delivered lifecycle operation and its exact result envelope."""
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type=kind,
        operation_payload={"workspace_id": str(workspace.id)},
    )
    command = LifecycleCommand.objects.get(task=task)
    return task, OperationRepository.envelope(
        command, {"task_id": str(task.id), "workspace_id": str(workspace.id)}
    )


def runner_failure(data: dict, **fields) -> dict:
    """A sanitized runner journal failure, as published after ``finish``."""
    known = fields.get("outcome_known", True)
    return {
        **data,
        "error": "Operation failed; execution finished"
        if known
        else "Operation interrupted; resources preserved for retry",
        "execution_finished": True,
        **fields,
    }


def toasts(frontend) -> list[str]:
    return [
        call.args[1]["error"]
        for call in frontend.await_args_list
        if call.args[0] == "workspace:error"
    ]


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"diagnostic_code": "disk_shrink_unsupported"}, SHRINK),
        (
            {"diagnostic_code": "insufficient_storage", "outcome_known": False},
            f"{STORAGE} Automatic recovery is checking the runner state.",
        ),
        ({"diagnostic_code": "handler_failed"}, FAILED),
        ({"diagnostic_code": "interrupted", "outcome_known": False}, INTERRUPTED),
        ({"diagnostic_code": "/var/lib/secret", "error": "/var/lib/secret"}, FAILED),
        ({}, FAILED),
    ],
)
def test_failure_message_translates_codes_never_runner_text(result, message):
    assert failure_message(result) == message


def test_recovered_message_keeps_coded_cause():
    coded = {"diagnostic_code": "insufficient_storage", "outcome_known": False}
    assert recovered_message(coded) == f"{STORAGE} {STORAGE_ACTION}"
    assert recovered_message({"diagnostic_code": "interrupted"}) == RECOVERED
    assert recovered_message(None) == RECOVERED


def test_known_refusal_resolves_without_intervention(
    runner, stopped_workspace, frontend, django_capture_on_commit_callbacks
):
    task, data = lifecycle(runner, stopped_workspace, "resume_workspace")
    with django_capture_on_commit_callbacks(execute=True):
        assert apply_result(
            RunnerService(),
            str(runner.id),
            "workspace:error",
            runner_failure(
                data,
                diagnostic_code="disk_shrink_unsupported",
                observed_status="exited",
                outcome_known=True,
            ),
        )

    task.refresh_from_db()
    stopped_workspace.refresh_from_db()
    assert task.status == "failed" and task.error == SHRINK
    assert LifecycleCommand.objects.get(task=task).phase == "result"
    assert stopped_workspace.status == "stopped"
    assert stopped_workspace.current_task_id is None
    assert not stopped_workspace.intervention_required
    assert stopped_workspace.lifecycle_diagnostic == SHRINK
    assert toasts(frontend) == [SHRINK]


def test_failed_create_keeps_reason_until_next_operation(runner, user):
    workspace = Workspace.objects.create(
        runner=runner, name="Fresh", status="creating", created_by=user
    )
    _task, data = lifecycle(runner, workspace, "create_workspace")
    assert apply_result(
        RunnerService(),
        str(runner.id),
        "workspace:error",
        runner_failure(data, diagnostic_code="image_unavailable", outcome_known=True),
    )
    workspace.refresh_from_db()
    assert workspace.status == "failed"
    assert workspace.lifecycle_diagnostic == IMAGE

    # The next lifecycle attempt supersedes the stale failure reason.
    TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="remove_workspace",
    )
    workspace.refresh_from_db()
    assert workspace.lifecycle_diagnostic == ""


def test_unknown_outcome_keeps_coded_reason_through_recovery(
    runner, workspace, frontend, django_capture_on_commit_callbacks
):
    task, data = lifecycle(runner, workspace, "stop_workspace")
    failure = runner_failure(
        data, diagnostic_code="insufficient_storage", outcome_known=False
    )
    service = RunnerService()
    with django_capture_on_commit_callbacks(execute=True):
        assert not apply_result(service, str(runner.id), "workspace:error", failure)

    workspace.refresh_from_db()
    pending = f"{STORAGE} Automatic recovery is checking the runner state."
    assert workspace.intervention_required
    assert workspace.lifecycle_diagnostic == pending
    assert toasts(frontend) == [pending]

    command = LifecycleCommand.objects.get(task=task)
    assert DispositionService.recover_inspection(
        service,
        str(runner.id),
        runner.sid,
        {
            "identity": OperationRepository.envelope(command, {}),
            "instance_id": "runner-journal",
            "status": "terminal",
            "execution_finished": True,
            "quiescent": True,
            "outcome_known": False,
            "event": "workspace:error",
            "result": failure,
            "workspace_status": "running",
        },
    )
    task.refresh_from_db()
    workspace.refresh_from_db()
    recovered = f"{STORAGE} {STORAGE_ACTION}"
    assert task.error == recovered
    assert workspace.current_task_id is None
    assert workspace.status == "running"
    assert workspace.lifecycle_diagnostic == recovered


def test_uncoded_interruption_keeps_generic_recovery_text(runner, workspace):
    task, data = lifecycle(runner, workspace, "stop_workspace")
    service = RunnerService()
    failure = runner_failure(data, diagnostic_code="interrupted", outcome_known=False)
    assert not apply_result(service, str(runner.id), "workspace:error", failure)
    task.refresh_from_db()
    assert task.error == INTERRUPTED
    command = LifecycleCommand.objects.get(task=task)
    assert DispositionService.recover_inspection(
        service,
        str(runner.id),
        runner.sid,
        {
            "identity": OperationRepository.envelope(command, {}),
            "instance_id": "runner-journal",
            "status": "terminal",
            "execution_finished": True,
            "quiescent": True,
            "outcome_known": False,
            "result": failure,
            "workspace_status": "running",
        },
    )
    workspace.refresh_from_db()
    assert workspace.lifecycle_diagnostic == RECOVERED


@pytest.mark.django_db(transaction=True)
def test_only_the_owner_reads_the_retained_diagnostic(runner, stopped_workspace, user):
    Membership.objects.create(
        user=user, organization=runner.organization, role="member"
    )
    stranger = get_user_model().objects.create_user(
        email=f"stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=stranger, organization=runner.organization, role="member"
    )
    task, data = lifecycle(runner, stopped_workspace, "resume_workspace")
    assert apply_result(
        RunnerService(),
        str(runner.id),
        "workspace:error",
        runner_failure(data, diagnostic_code="disk_shrink_unsupported"),
    )
    permissions = [APIKeyPermission.WORKSPACES_READ, APIKeyPermission.RUNNERS_READ]
    owner = _make_client(user=user, org=runner.organization, permissions=permissions)
    other = _make_client(
        user=stranger, org=runner.organization, permissions=permissions
    )
    workspace_url = f"/api/v1/workspaces/{stopped_workspace.id}/"
    operation_url = f"/api/v1/runners/operations/{task.id}/"

    response = owner.get(workspace_url)
    assert response.status_code == 200
    assert response.json()["lifecycle_diagnostic"] == SHRINK
    response = owner.get(operation_url)
    assert response.status_code == 200
    assert response.json()["diagnostic"] == SHRINK

    assert other.get(workspace_url).status_code == 404
    assert other.get(operation_url).status_code == 403
