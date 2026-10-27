"""Internal scheduled admission rollback works under lifecycle fences."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.utils import timezone

from apps.accounts.models import User
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessSession
from apps.organizations.models import Membership, Organization
from apps.runners.models import Runner, Task, Workspace
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskService


@pytest.fixture
def schedule(db):
    org = Organization.objects.create(name="Cleanup", slug=uuid.uuid4().hex)
    owner = User.objects.create_user(email=f"{uuid.uuid4().hex}@example.com")
    Membership.objects.create(user=owner, organization=org)
    runner = Runner.objects.create(
        organization=org, status="online", api_token_hash=uuid.uuid4().hex
    )
    workspace = Workspace.objects.create(
        runner=runner, created_by=owner, name="Cleanup", status="running"
    )
    return ScheduledTask.objects.create(
        organization=org,
        owner=owner,
        workspace=workspace,
        name="Cleanup",
        prompt="Review",
        local_time="09:00",
        timezone_name="UTC",
        next_run_at=timezone.now(),
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "operation,reason",
    [
        ("capturing_image", "workspace_operation_active"),
        (None, "workspace_lifecycle_unresolved"),
    ],
)
def test_real_harness_admission_rollback_under_lifecycle_fence(
    schedule, operation, reason, monkeypatch
):
    harness = HarnessService()
    notify = AsyncMock()
    monkeypatch.setattr(harness.conversation_events, "emit", notify)
    start = harness.start_run
    lifecycle = Task.objects.create(
        runner=schedule.workspace.runner, type="create_image_artifact", status="failed"
    )
    created = []

    async def fenced_start(session, prompt, **kwargs):
        created.append(session.id)
        await sync_to_async(Workspace.objects.filter(pk=schedule.workspace_id).update)(
            active_operation=operation, current_task_id=lifecycle.id
        )
        return await start(session, prompt, **kwargs)

    monkeypatch.setattr(harness, "start_run", fenced_start)
    run = async_to_sync(ScheduledTaskService(harness=harness).run_now)(schedule)
    run.refresh_from_db()
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == reason
    assert run.session_id is None
    assert not HarnessSession.objects.filter(pk=created[0]).exists()
    assert not HarnessMessage.objects.filter(session_id=created[0]).exists()
    assert notify.await_count == 2


def _claimed_chat(schedule):
    session = HarnessSession.objects.create(
        workspace=schedule.workspace, organization_id=schedule.organization_id
    )
    run = ScheduledTaskRun.objects.create(
        scheduled_task=schedule, scheduled_for=timezone.now(), session=session
    )
    return session, run


def _cleanup(schedule, session, run):
    notify = AsyncMock()
    harness = SimpleNamespace(conversation_events=SimpleNamespace(emit=notify))
    async_to_sync(ScheduledTaskService()._cleanup_unstarted_session)(
        harness, session, run, schedule
    )
    return notify


@pytest.mark.django_db
def test_cleanup_discards_unstarted_chat_and_detaches_claim(schedule):
    session, run = _claimed_chat(schedule)

    notify = _cleanup(schedule, session, run)

    assert not HarnessSession.objects.filter(pk=session.pk).exists()
    assert run.session is None
    run.refresh_from_db()
    assert run.session_id is None
    assert run.status == ScheduledTaskRun.Status.CLAIMED
    notify.assert_awaited_once_with(schedule.workspace_id)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "preserve",
    ["message", "busy", "started", "other_claim", "wrong_claim", "child", "foreign"],
)
def test_cleanup_preserves_real_or_unowned_sessions(schedule, preserve):
    session, run = _claimed_chat(schedule)
    claim = run
    if preserve == "message":
        HarnessMessage.objects.create(session=session, role="user", content="Real chat")
    elif preserve == "busy":
        HarnessSession.objects.filter(pk=session.pk).update(status="busy")
    elif preserve == "started":
        ScheduledTaskRun.objects.filter(pk=run.pk).update(started_at=timezone.now())
    elif preserve == "other_claim":
        ScheduledTaskRun.objects.create(
            scheduled_task=schedule, scheduled_for=timezone.now(), session=session
        )
    elif preserve == "wrong_claim":
        claim = SimpleNamespace(id=uuid.uuid4(), session=session)
    elif preserve == "child":
        HarnessSession.objects.create(
            workspace=schedule.workspace,
            organization_id=schedule.organization_id,
            parent=session,
        )
    else:
        other = Organization.objects.create(name="Other", slug=uuid.uuid4().hex)
        HarnessSession.objects.filter(pk=session.pk).update(organization_id=other.id)

    notify = _cleanup(schedule, session, claim)

    assert HarnessSession.objects.filter(pk=session.pk).exists()
    run.refresh_from_db()
    assert run.session_id == session.id
    notify.assert_not_awaited()
