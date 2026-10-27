"""Scheduled launches against real workspace, chat and auto-stop state."""

# Imported pytest fixture names intentionally match injected arguments.
# ruff: noqa: F811

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.harness.models import HarnessMessage, HarnessSession
from apps.organizations.models import Membership, Organization
from apps.runners.models import Runner, Workspace
from apps.runners.services import RunnerService
from apps.scheduled_tasks.models import ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskService
from apps.scheduled_tasks.tests.test_service_api import (
    scheduled_run_setup,  # noqa: F401
)


class UnexpectedHarness:
    def create_session(self, **values):
        pytest.fail("an unavailable workspace must not start a chat")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "change", ["other_creator", "other_org", "no_membership", "inactive_owner"]
)
def test_launch_skips_workspace_its_owner_no_longer_reaches(
    scheduled_run_setup, change
):
    org, owner, outsider, workspace, task = scheduled_run_setup
    if change == "other_creator":
        Workspace.objects.filter(pk=workspace.pk).update(created_by=outsider)
    elif change == "other_org":
        other = Organization.objects.create(name="Other", slug=uuid.uuid4().hex)
        Runner.objects.filter(pk=workspace.runner_id).update(organization=other)
    elif change == "no_membership":
        Membership.objects.filter(user=owner, organization=org).delete()
    else:
        get_user_model().objects.filter(pk=owner.pk).update(is_active=False)

    run = asyncio.run(ScheduledTaskService(harness=UnexpectedHarness()).run_now(task))

    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "workspace_unavailable"
    assert not HarnessSession.objects.filter(workspace=workspace).exists()


@pytest.mark.django_db(transaction=True)
def test_started_launch_marks_workspace_active(scheduled_run_setup):
    org, _, _, workspace, task = scheduled_run_setup
    stale = timezone.now() - timedelta(hours=1)
    Workspace.objects.filter(pk=workspace.pk).update(last_activity_at=stale)

    class Harness:
        def create_session(self, **values):
            return HarnessSession.objects.create(
                workspace_id=values["workspace_id"],
                organization_id=values["organization_id"],
            )

        async def start_run(self, session, prompt, **values):
            return await sync_to_async(HarnessMessage.objects.create)(
                session=session, role="assistant"
            )

    run = asyncio.run(ScheduledTaskService(harness=Harness()).run_now(task))

    assert run.status == ScheduledTaskRun.Status.RUNNING
    workspace.refresh_from_db()
    assert workspace.last_activity_at > stale


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "state,stops",
    [
        ("idle", True),
        ("recently_active", False),
        ("busy_chat", False),
        ("stopped", False),
        ("disabled", False),
    ],
)
def test_completed_run_applies_workspace_auto_stop_policy(
    scheduled_run_setup, state, stops
):
    org, _, _, workspace, task = scheduled_run_setup
    org.workspace_auto_stop_timeout_minutes = None if state == "disabled" else 5
    org.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    idle_for = timedelta(minutes=1 if state == "recently_active" else 10)
    Workspace.objects.filter(pk=workspace.pk).update(
        last_activity_at=timezone.now() - idle_for,
        status="stopped" if state == "stopped" else "running",
    )
    if state == "busy_chat":
        HarnessSession.objects.create(
            workspace=workspace, organization_id=org.id, status="busy"
        )
    ScheduledTaskRun.objects.create(
        scheduled_task=task,
        scheduled_for=timezone.now(),
        status=ScheduledTaskRun.Status.SUCCEEDED,
        completion_check_pending=True,
    )
    runner = RunnerService()
    runner.stop_workspace = AsyncMock()

    asyncio.run(ScheduledTaskService(runner=runner).reconcile_runs())

    if stops:
        runner.stop_workspace.assert_awaited_once_with(workspace.id, auto_stop=True)
    else:
        runner.stop_workspace.assert_not_awaited()
    assert not ScheduledTaskRun.objects.filter(completion_check_pending=True).exists()
