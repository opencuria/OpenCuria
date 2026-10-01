import uuid
from datetime import datetime, timezone

import pytest

from apps.accounts.models import User
from apps.harness.models import HarnessMessage, HarnessSession
from apps.organizations.models import Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.repositories import ScheduledTaskRepository


@pytest.mark.django_db
def test_restart_marks_only_scheduled_run_interrupted_and_closes_its_chat():
    org = Organization.objects.create(
        name="Recovery", slug=f"recovery-{uuid.uuid4().hex}"
    )
    owner = User.objects.create_user(email=f"recover-{uuid.uuid4().hex}@example.com")
    runner = Runner.objects.create(
        organization=org, api_token_hash=uuid.uuid4().hex, status=RunnerStatus.ONLINE
    )
    workspace = Workspace.objects.create(
        runner=runner, created_by=owner, name="recovery", status=WorkspaceStatus.RUNNING
    )
    schedule = ScheduledTask.objects.create(
        organization=org,
        owner=owner,
        workspace=workspace,
        name="daily",
        prompt="Review",
        recurrence="daily",
        local_time="09:00",
        timezone_name="UTC",
        next_run_at=datetime.now(timezone.utc),
    )
    session = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, mode="build", agent_name="build"
    )
    assistant = HarnessMessage.objects.create(session=session, role="assistant")
    run = ScheduledTaskRun.objects.create(
        scheduled_task=schedule,
        scheduled_for=datetime.now(timezone.utc),
        session=session,
        assistant_message=assistant,
        status=ScheduledTaskRun.Status.RUNNING,
    )
    unrelated = HarnessSession.objects.create(
        workspace=workspace,
        organization_id=org.id,
        mode="build",
        agent_name="build",
        status="busy",
    )

    assert ScheduledTaskRepository.recover_backend_restart() == 1
    run.refresh_from_db()
    session.refresh_from_db()
    assistant.refresh_from_db()
    unrelated.refresh_from_db()
    assert run.status == ScheduledTaskRun.Status.INTERRUPTED
    assert session.status == "idle"
    assert assistant.finish == "aborted"
    assert unrelated.status == "busy"
