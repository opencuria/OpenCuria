"""Runner attention reflects unresolved workflows, not historical task failures."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import Membership
from apps.runners.models import (
    CaptureRequest,
    ImageDeletionRequest,
    ImageInstance,
    LifecycleCommand,
    Runner,
    Task,
    Workspace,
    WorkspaceRecreateRequest,
)
from apps.runners.services.disposition import DispositionService
from apps.runners.services.storage import StorageService
from apps.runners.storage_schemas import RunnerStorageOut
from common.exceptions import AuthenticationError

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("evidence", "actions", "message"),
    [
        ({"status": "running"}, [], "still running"),
        ({"status": "unknown"}, [], "could not verify"),
        (
            {"status": "terminal"},
            ["acknowledge_interrupted"],
            "keep existing resources",
        ),
        ({"status": "terminal"}, ["reconcile", "retry"], "operation failed"),
        (
            {"result": {"error": "SSH reconnect failed"}},
            [],
            "workspace is not reachable",
        ),
    ],
)
def test_review_explains_the_actual_next_step(
    evidence: dict, actions: list[str], message: str
) -> None:
    assert message in DispositionService.review_message(evidence, actions)


def test_rest_and_mcp_expose_the_same_grouped_activity(
    runner: Runner, workspace: Workspace, user: User
) -> None:
    """The shared storage service must serialize identically in both interfaces."""
    from apps.accounts.models import APIKeyPermission
    from apps.mcp_app.server import _call_get_runner_storage
    from apps.runners.tests.test_workspace_access_controls import _make_client

    command(workspace, "intervention")
    detail(user, runner)
    client = _make_client(
        user=user,
        org=runner.organization,
        permissions=[APIKeyPermission.RUNNERS_READ],
    )
    response = client.get(f"/api/v1/runners/{runner.id}/storage/")
    assert response.status_code == 200
    mcp = _call_get_runner_storage(
        SimpleNamespace(user=user),
        runner.organization_id,
        {"runner_id": str(runner.id)},
    )
    # REST timestamps use millisecond precision; compare the activity contract.
    rest_activities = [
        activity.model_dump(exclude={"created_at"})
        for activity in RunnerStorageOut.model_validate(response.json()).activities
    ]
    mcp_activities = [
        activity.model_dump(exclude={"created_at"})
        for activity in RunnerStorageOut.model_validate(
            json.loads(mcp[0].text)
        ).activities
    ]
    assert rest_activities == mcp_activities
    assert response.json()["activities"][0]["state"] == "attention"
    assert "must-not-be-returned" not in response.content.decode()


def command(workspace: Workspace, phase: str, status: str = "failed") -> Task:
    """Seed execution facts without performing a runtime action."""
    task = Task.objects.create(
        workspace=workspace,
        runner=workspace.runner,
        type="stop_workspace",
        status=status,
        error="Unknown outcome" if status == "failed" else "",
    )
    LifecycleCommand.objects.create(
        task=task,
        phase=phase,
        target=str(workspace.id),
        deadline_at=timezone.now(),
        payload={"files": {"private": "must-not-be-returned"}},
    )
    if phase == "intervention":
        workspace.current_task = task
        workspace.save(update_fields=["current_task"])
    return task


def detail(user: User, runner: Runner) -> dict:
    """Use the same authorized service as REST and MCP."""
    Membership.objects.get_or_create(
        user=user,
        organization=runner.organization,
        defaults={"role": "admin"},
    )
    return StorageService().detail(user, runner.organization_id, runner.id)


def test_only_blender_needs_attention_and_closed_capture_is_one_history_entry(
    runner: Runner, workspace: Workspace, user: User
) -> None:
    workspace.name = "OpenCuria 3"
    workspace.save(update_fields=["name"])
    closed = command(workspace, "disposed")
    image = ImageInstance.objects.create(
        runner=runner,
        name="OpenCuria",
        runtime_type="qemu",
        status="failed",
        origin_type="workspace_capture",
        is_legacy=True,
    )
    CaptureRequest.objects.create(
        workspace=workspace,
        image=image,
        child=closed,
        phase="failed",
        resume_suppressed=True,
        diagnostic="Interrupted child acknowledged",
    )
    blender = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Blender",
        status="running",
    )
    unresolved = command(blender, "intervention")
    result = detail(user, runner)
    attention = [a for a in result["activities"] if a["state"] == "attention"]
    history = [a for a in result["activities"] if a["state"] == "history"]
    assert len(attention) == 1
    assert attention[0]["target_name"] == "Blender"
    assert attention[0]["operation_ids"] == [str(unresolved.id)]
    assert len(history) == 1
    assert history[0]["target_name"] == "OpenCuria 3"
    assert history[0]["outcome"] == "resolved"
    assert history[0]["operation_ids"] == []
    # Presentation must never release the real fence or rewrite execution results.
    blender.refresh_from_db()
    closed.refresh_from_db()
    assert blender.current_task_id == unresolved.id
    assert closed.status == "failed"
    serialized = RunnerStorageOut.model_validate(result).model_dump_json()
    assert "must-not-be-returned" not in serialized
    assert "recreate_requests" not in serialized


@pytest.mark.parametrize(
    ("phase", "status", "state", "outcome"),
    [
        ("disposed", "failed", "history", "resolved"),
        ("result", "failed", "history", "failed"),
        ("intervention", "failed", "attention", "blocked"),
        ("delivery", "pending", "active", "in_progress"),
    ],
)
def test_execution_failure_is_separate_from_unresolved_outcome(
    runner: Runner,
    workspace: Workspace,
    user: User,
    phase: str,
    status: str,
    state: str,
    outcome: str,
) -> None:
    command(workspace, phase, status)
    activity = detail(user, runner)["activities"][0]
    assert activity["state"] == state
    assert activity["outcome"] == outcome


def test_failed_capture_with_retained_child_stays_visible_and_counted_once(
    runner: Runner, workspace: Workspace, user: User
) -> None:
    task = command(workspace, "intervention")
    image = ImageInstance.objects.create(
        runner=runner,
        name="Capture",
        runtime_type="qemu",
        status="failed",
        origin_type="workspace_capture",
        is_legacy=True,
    )
    CaptureRequest.objects.create(
        workspace=workspace,
        image=image,
        phase="failed",
        child=task,
        resume_suppressed=True,
    )
    activities = detail(user, runner)["activities"]
    assert len(activities) == 1
    assert activities[0]["kind"] == "capture"
    assert activities[0]["state"] == "attention"
    assert activities[0]["operation_ids"] == [str(task.id)]


def test_capture_replacement_is_part_of_the_same_workflow(
    runner: Runner, workspace: Workspace, user: User
) -> None:
    """Saving and applying an image must not appear as unrelated activities."""
    completed = command(workspace, "result", "completed")
    image = ImageInstance.objects.create(
        runner=runner,
        name="Capture",
        runtime_type="qemu",
        status="ready",
        origin_type="workspace_capture",
        is_legacy=True,
    )
    capture = CaptureRequest.objects.create(
        workspace=workspace,
        image=image,
        phase="completed",
        child=completed,
    )
    blocked = command(workspace, "intervention")
    WorkspaceRecreateRequest.objects.create(
        workspace=workspace,
        reason="capture",
        child=blocked,
        phase="intervention",
    )
    activities = detail(user, runner)["activities"]
    assert len(activities) == 1
    assert activities[0]["id"] == str(capture.id)
    assert activities[0]["state"] == "attention"
    assert activities[0]["operation_ids"] == [str(blocked.id)]


def test_deletion_approval_and_child_are_one_open_activity(
    runner: Runner, workspace: Workspace, user: User
) -> None:
    task = command(workspace, "intervention")
    deletion = ImageDeletionRequest.objects.create(
        organization=runner.organization,
        requested_by=user,
        target_type="image",
        target_id=uuid.uuid4(),
        phase="intervention",
        children={"workspace": str(task.id)},
        approval={
            "images": [],
            "workspaces": [
                {
                    "id": str(workspace.id),
                    "name": workspace.name,
                    "runner_id": str(runner.id),
                }
            ],
        },
    )
    activities = detail(user, runner)["activities"]
    assert len(activities) == 1
    assert activities[0]["id"] == str(deletion.id)
    assert activities[0]["state"] == "attention"
    deletion.phase = "cancelled"
    deletion.save(update_fields=["phase"])
    LifecycleCommand.objects.filter(task=task).update(phase="disposed")
    activities = detail(user, runner)["activities"]
    assert activities[0]["state"] == "history"
    assert activities[0]["outcome"] == "cancelled"


def test_activity_does_not_leak_to_members_or_another_runner(
    runner: Runner, workspace: Workspace, user: User, offline_runner: Runner
) -> None:
    command(workspace, "intervention")
    membership = Membership.objects.create(
        user=user,
        organization=runner.organization,
        role="member",
    )
    with pytest.raises(AuthenticationError):
        StorageService().detail(user, runner.organization_id, runner.id)
    membership.role = "admin"
    membership.save(update_fields=["role"])
    assert (
        StorageService().detail(
            user,
            runner.organization_id,
            offline_runner.id,
        )["activities"]
        == []
    )
