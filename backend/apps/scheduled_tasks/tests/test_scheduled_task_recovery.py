from __future__ import annotations

import uuid
from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.harness.models import (
    HarnessMessage,
    HarnessPart,
    HarnessSession,
    QuestionRequest,
)
from apps.harness.permissions.models import PermissionRequest
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.repositories import ScheduledTaskRepository


def make_schedule():
    organization = Organization.objects.create(
        name="Scheduled test", slug=f"scheduled-{uuid.uuid4().hex}"
    )
    owner = User.objects.create_user(email=f"{uuid.uuid4().hex}@example.com")
    Membership.objects.create(
        user=owner, organization=organization, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        organization=organization,
        api_token_hash=uuid.uuid4().hex,
        status=RunnerStatus.ONLINE,
    )
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=owner,
        name="scheduled workspace",
        status=WorkspaceStatus.RUNNING,
    )
    task = ScheduledTask.objects.create(
        organization=organization,
        owner=owner,
        workspace=workspace,
        name="Daily review",
        prompt="Original prompt",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
        recurrence="daily",
        weekdays=[],
        local_time=time(9),
        timezone_name="UTC",
        next_run_at=timezone.now() + timedelta(hours=1),
    )
    return owner, organization, workspace, task


@pytest.mark.django_db(transaction=True)
def test_claim_snapshots_locked_task_configuration_not_stale_due_object():
    _, _, _, task = make_schedule()
    due = task.next_run_at
    stale_task = ScheduledTask.objects.get(id=task.id)
    ScheduledTask.objects.filter(id=task.id).update(
        prompt="Edited prompt",
        mode="plan",
        model="",
        reasoning_effort="",
        harness_id="claude",
    )

    run = ScheduledTaskRepository.claim(
        stale_task, scheduled_for=due, next_run_at=due + timedelta(days=1)
    )

    assert run is not None
    assert run.configuration_snapshot["prompt"] == "Edited prompt"
    assert run.configuration_snapshot["mode"] == "plan"
    assert run.configuration_snapshot["model"] == ""
    assert run.configuration_snapshot["reasoning_effort"] == ""
    assert run.configuration_snapshot["harness_id"] == "claude"
    assert run.configuration_snapshot["workspace_id"] == str(task.workspace_id)
    assert "owner_id" not in run.configuration_snapshot
    assert run.trigger == "scheduled"


@pytest.mark.django_db(transaction=True)
def test_manual_claim_snapshots_configuration_and_marks_trigger():
    _, _, _, task = make_schedule()

    run = ScheduledTaskRepository.create_manual_run(task, timezone.now())

    assert run.trigger == "manual"
    assert run.configuration_snapshot["prompt"] == "Original prompt"
    assert run.configuration_snapshot["model"] == ""


@pytest.mark.django_db(transaction=True)
def test_restart_recovery_keeps_terminal_result_and_cleans_only_lost_run():
    _, organization, workspace, task = make_schedule()
    now = timezone.now()
    finished_session = HarnessSession.objects.create(
        workspace=workspace,
        organization_id=organization.id,
        title="Finished scheduled run",
        mode="build",
        agent_name="build",
        status="idle",
    )
    finished_message = HarnessMessage.objects.create(
        session=finished_session,
        role="assistant",
        content="Done",
        finish="stop",
        completed_at=now,
    )
    finished_run = ScheduledTaskRun.objects.create(
        scheduled_task=task,
        scheduled_for=task.next_run_at,
        status=ScheduledTaskRun.Status.RUNNING,
        session=finished_session,
        assistant_message=finished_message,
    )

    root = HarnessSession.objects.create(
        workspace=workspace,
        organization_id=organization.id,
        title="Interrupted scheduled run",
        mode="build",
        agent_name="build",
        status="busy",
    )
    lost_message = HarnessMessage.objects.create(
        session=root, role="assistant", content="", position=0
    )
    followup_message = HarnessMessage.objects.create(
        session=root, role="assistant", content="", position=1
    )
    lost_run = ScheduledTaskRun.objects.create(
        scheduled_task=task,
        scheduled_for=task.next_run_at + timedelta(days=1),
        status=ScheduledTaskRun.Status.RUNNING,
        session=root,
        assistant_message=lost_message,
    )
    child = HarnessSession.objects.create(
        workspace=workspace,
        parent=root,
        organization_id=organization.id,
        title="Subagent",
        mode="build",
        agent_name="general",
        status="busy",
    )
    child_message = HarnessMessage.objects.create(
        session=child, role="assistant", content="", position=0
    )
    HarnessPart.objects.create(
        message=lost_message,
        type="subtask",
        meta={"child_session_id": str(child.id)},
    )
    permission = PermissionRequest.objects.create(
        organization_id=organization.id,
        workspace_id=workspace.id,
        session_id=child.id,
        message_id=child_message.id,
        tool="terminal",
        pattern="*",
    )
    question = QuestionRequest.objects.create(
        organization_id=organization.id,
        workspace_id=workspace.id,
        session_id=child.id,
        message_id=child_message.id,
        questions=[],
    )

    assert ScheduledTaskRepository.recover_backend_restart() == 2

    finished_run.refresh_from_db()
    lost_run.refresh_from_db()
    lost_message.refresh_from_db()
    followup_message.refresh_from_db()
    root.refresh_from_db()
    child.refresh_from_db()
    child_message.refresh_from_db()
    permission.refresh_from_db()
    question.refresh_from_db()
    assert finished_run.status == ScheduledTaskRun.Status.SUCCEEDED
    assert finished_run.finished_at == now
    assert lost_run.status == ScheduledTaskRun.Status.INTERRUPTED
    assert lost_message.finish == "aborted"
    assert lost_message.completed_at is not None
    assert followup_message.completed_at is None
    assert root.status == "busy"
    assert child.status == "idle"
    assert child_message.finish == "aborted"
    assert permission.status == "rejected"
    assert question.status == "rejected"
