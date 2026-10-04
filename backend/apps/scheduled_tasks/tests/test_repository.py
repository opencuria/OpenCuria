import uuid
from datetime import datetime, timezone

import pytest

from apps.accounts.models import User
from apps.organizations.models import Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Task, Workspace
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.repositories import ScheduledTaskRepository


@pytest.mark.django_db
def test_claim_is_unique_and_advances_schedule_atomically():
    organization = Organization.objects.create(
        name="Org", slug=f"org-{uuid.uuid4().hex}"
    )
    owner = User.objects.create_user(
        email=f"{uuid.uuid4().hex}@example.com", password="secret"
    )
    runner = Runner.objects.create(
        organization=organization,
        api_token_hash=uuid.uuid4().hex,
        status=RunnerStatus.ONLINE,
    )
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=owner,
        name="test",
        status=WorkspaceStatus.RUNNING,
    )
    scheduled_for = datetime(2025, 1, 1, 9, tzinfo=timezone.utc)
    next_run = datetime(2025, 1, 2, 9, tzinfo=timezone.utc)
    schedule = ScheduledTask.objects.create(
        organization=organization,
        owner=owner,
        workspace=workspace,
        name="daily",
        prompt="Inspect the repo",
        recurrence="daily",
        local_time="09:00",
        timezone_name="UTC",
        enabled=True,
        next_run_at=scheduled_for,
    )

    claimed = ScheduledTaskRepository.claim(
        schedule, scheduled_for=scheduled_for, next_run_at=next_run
    )
    assert claimed is not None
    assert (
        ScheduledTaskRepository.claim(
            schedule, scheduled_for=scheduled_for, next_run_at=next_run
        )
        is None
    )
    schedule.refresh_from_db()
    assert schedule.next_run_at == next_run
    assert schedule.runs.count() == 1


@pytest.mark.django_db
def test_owner_queries_are_scoped_by_owner_and_organization():
    organization = Organization.objects.create(
        name="Org", slug=f"org-{uuid.uuid4().hex}"
    )
    owner = User.objects.create_user(
        email=f"{uuid.uuid4().hex}@example.com", password="secret"
    )
    other = User.objects.create_user(
        email=f"{uuid.uuid4().hex}@example.com", password="secret"
    )
    runner = Runner.objects.create(
        organization=organization, api_token_hash=uuid.uuid4().hex
    )
    workspace = Workspace.objects.create(
        runner=runner, created_by=owner, name="workspace"
    )
    schedule = ScheduledTask.objects.create(
        organization=organization,
        owner=owner,
        workspace=workspace,
        name="daily",
        prompt="Inspect the repo",
        recurrence="daily",
        local_time="09:00",
        timezone_name="UTC",
        next_run_at=datetime.now(timezone.utc),
    )

    assert (
        ScheduledTaskRepository.get_for_owner(
            schedule.id, organization_id=organization.id, owner_id=owner.id
        )
        == schedule
    )
    assert (
        ScheduledTaskRepository.get_for_owner(
            schedule.id, organization_id=organization.id, owner_id=other.id
        )
        is None
    )
    run = ScheduledTaskRun.objects.create(
        scheduled_task=schedule,
        scheduled_for=datetime.now(timezone.utc),
        status=ScheduledTaskRun.Status.SUCCEEDED,
    )
    ScheduledTaskRepository.delete(schedule)
    run.refresh_from_db()
    assert run.scheduled_task_id == schedule.id
    assert ScheduledTaskRepository.list_runs(
        schedule, limit=100
    ) == [run]
    assert (
        ScheduledTaskRepository.get_for_owner(
            schedule.id, organization_id=organization.id, owner_id=owner.id
        )
        is None
    )


@pytest.mark.django_db
def test_workspace_resume_snapshot_includes_unresolved_lifecycle_task():
    organization = Organization.objects.create(
        name="Org", slug=f"org-{uuid.uuid4().hex}"
    )
    owner = User.objects.create_user(
        email=f"{uuid.uuid4().hex}@example.com", password="secret"
    )
    runner = Runner.objects.create(
        organization=organization,
        api_token_hash=uuid.uuid4().hex,
        status=RunnerStatus.ONLINE,
    )
    lifecycle_task = Task.objects.create(
        runner=runner, type="create_image_artifact", status="failed"
    )
    task_id = lifecycle_task.id
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=owner,
        name="intervention",
        status=WorkspaceStatus.RUNNING,
        current_task_id=task_id,
    )
    assert ScheduledTaskRepository.workspace_resume_state(workspace.id) == (
        WorkspaceStatus.RUNNING,
        None,
        RunnerStatus.ONLINE,
        True,
        task_id,
    )
    assert ScheduledTaskRepository.workspace_resume_state(uuid.uuid4()) == (
        None,
        None,
        None,
        False,
        None,
    )
