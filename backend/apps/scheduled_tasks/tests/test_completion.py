"""Durable assistant-to-ledger completion and deferred auto-stop regressions."""

# Imported pytest fixture names intentionally match injected arguments.
# ruff: noqa: F811

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.harness.models import HarnessMessage, HarnessSession
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.repositories import ScheduledTaskRepository
from apps.scheduled_tasks.services import ScheduledTaskService
from apps.scheduled_tasks.tests.test_service_api import (
    scheduled_run_setup,  # noqa: F401
)


@pytest.fixture
def completed_run(scheduled_run_setup):
    org, _, _, workspace, task = scheduled_run_setup
    session = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, mode="build", agent_name="build"
    )
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", completed_at=timezone.now(), finish="stop"
    )
    return ScheduledTaskRun.objects.create(
        scheduled_task=task,
        scheduled_for=timezone.now(),
        session=session,
        assistant_message=assistant,
        status=ScheduledTaskRun.Status.RUNNING,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "finish,error,expected",
    [
        ("stop", "", "succeeded"),
        ("error", "", "error"),
        ("stop", "provider failed", "error"),
        ("aborted", "aborted by user", "error"),
    ],
)
def test_completion_maps_persisted_assistant_outcome(
    completed_run, finish, error, expected
):
    run = completed_run
    HarnessMessage.objects.filter(id=run.assistant_message_id).update(
        finish=finish, error=error
    )
    ScheduledTaskService().complete_assistant_run(run.assistant_message_id)
    run.refresh_from_db()
    assert run.status == expected
    assert run.error == error
    assert run.finished_at == run.assistant_message.completed_at
    assert run.completion_check_pending


@pytest.mark.django_db
def test_list_repairs_only_requested_task_and_leaves_incomplete_runs(completed_run):
    run = completed_run
    task = run.scheduled_task
    other_task = ScheduledTask.objects.get(id=task.id)
    other_task.pk = None
    other_task.save()
    other = ScheduledTaskRun.objects.create(
        scheduled_task=other_task,
        scheduled_for=timezone.now(),
        assistant_message=run.assistant_message,
        status="running",
    )
    incomplete = HarnessMessage.objects.create(
        session=run.session, role="assistant", position=1
    )
    unfinished = ScheduledTaskRun.objects.create(
        scheduled_task=task,
        scheduled_for=timezone.now() + timedelta(seconds=1),
        assistant_message=incomplete,
        status="running",
    )
    service = ScheduledTaskService()
    rows = {row.id: row for row in service.list_runs(task)}
    assert rows[run.id].status == "succeeded"
    assert rows[unfinished.id].status == "running"
    assert rows[unfinished.id].finished_at is None
    other.refresh_from_db()
    assert other.status == "running"
    assert not other.completion_check_pending
    service.complete_assistant_run(incomplete.id)
    unfinished.refresh_from_db()
    assert unfinished.status == "running"
    assert not unfinished.completion_check_pending


@pytest.mark.django_db
def test_completion_is_conditional_idempotent_and_survives_deletion(completed_run):
    run = completed_run
    stale = ScheduledTaskRepository.active_runs(message_id=run.assistant_message_id)[0]
    service = ScheduledTaskService()
    service.complete_assistant_run(run.assistant_message_id)
    run.refresh_from_db()
    original = (run.status, run.finished_at, run.error)
    ScheduledTaskRepository.clear_completion_check(run.id)
    HarnessMessage.objects.filter(id=run.assistant_message_id).update(
        finish="error", error="late error", completed_at=timezone.now()
    )
    assert not ScheduledTaskRepository.finish_run(
        stale, status="error", finished_at=timezone.now(), error="stale writer"
    )
    service.complete_assistant_run(run.assistant_message_id)
    service.delete(run.scheduled_task)
    HarnessMessage.objects.filter(id=run.assistant_message_id).delete()
    HarnessSession.objects.filter(id=run.session_id).delete()
    run.refresh_from_db()
    assert (run.status, run.finished_at, run.error) == original
    assert not run.completion_check_pending
    assert run.assistant_message_id is None and run.session_id is None
    assert service.list_runs(run.scheduled_task)[0].status == "succeeded"


@pytest.mark.django_db
def test_finish_rejects_stale_assistant_link(completed_run):
    run = completed_run
    replacement = HarnessMessage.objects.create(
        session=run.session, role="assistant", position=1
    )
    ScheduledTaskRun.objects.filter(id=run.id).update(assistant_message=replacement)
    assert not ScheduledTaskRepository.finish_run(
        run, status="succeeded", finished_at=timezone.now(), error=""
    )
    run.refresh_from_db()
    assert run.status == "running"
    assert run.finished_at is None and not run.completion_check_pending


@pytest.mark.django_db(transaction=True)
def test_completion_before_link_rechecks_database_not_stale_returned_assistant(
    scheduled_run_setup,
):
    org, _, _, workspace, task = scheduled_run_setup
    session = HarnessSession.objects.create(workspace=workspace, organization_id=org.id)
    assistant = HarnessMessage.objects.create(session=session, role="assistant")
    run = ScheduledTaskRun.objects.create(
        scheduled_task=task, scheduled_for=timezone.now()
    )
    service = ScheduledTaskService()

    class CompletedBeforeLinkHarness:
        def create_session(self, **kwargs):
            return session

        async def start_run(self, *args, **kwargs):
            await sync_to_async(HarnessMessage.objects.filter(id=assistant.id).update)(
                completed_at=timezone.now(), finish="stop"
            )
            await sync_to_async(service.complete_assistant_run)(assistant.id)
            assert assistant.completed_at is None
            return assistant

    asyncio.run(service._create_and_start_run(task, run, CompletedBeforeLinkHarness()))
    run.refresh_from_db()
    assert run.assistant_message_id == assistant.id
    assert run.status == "succeeded"
    assert run.finished_at == run.assistant_message.completed_at
    assert run.completion_check_pending


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("retry", [False, True])
def test_eager_completion_defers_one_shot_auto_stop_and_retries_errors(
    completed_run, retry
):
    run = completed_run
    org = run.scheduled_task.organization
    org.workspace_auto_stop_timeout_minutes = 5
    org.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    workspace = run.scheduled_task.workspace
    workspace.last_activity_at = timezone.now() - timedelta(minutes=10)
    workspace.save(update_fields=["last_activity_at"])
    stop = AsyncMock(
        side_effect=[RuntimeError("runner unavailable"), None] if retry else None
    )
    service = ScheduledTaskService(runner=SimpleNamespace(stop_workspace=stop))
    service.complete_assistant_run(run.assistant_message_id)
    run.refresh_from_db()
    assert run.status == "succeeded" and run.completion_check_pending
    stop.assert_not_called()
    if retry:
        with pytest.raises(RuntimeError, match="runner unavailable"):
            asyncio.run(service.reconcile_runs())
        run.refresh_from_db()
        assert run.completion_check_pending
    asyncio.run(service.reconcile_runs())
    run.refresh_from_db()
    assert not run.completion_check_pending
    assert stop.await_count == (2 if retry else 1)
    stop.assert_awaited_with(workspace.id, auto_stop=True)
    asyncio.run(service.reconcile_runs())
    assert stop.await_count == (2 if retry else 1)


@pytest.mark.django_db(transaction=True)
def test_pending_auto_stop_noop_is_consumed_not_replayed_later(completed_run):
    run = completed_run
    org = run.scheduled_task.organization
    org.workspace_auto_stop_timeout_minutes = 0
    org.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    stop = AsyncMock()
    service = ScheduledTaskService(runner=SimpleNamespace(stop_workspace=stop))
    service.list_runs(run.scheduled_task)
    asyncio.run(service.reconcile_runs())
    run.refresh_from_db()
    assert not run.completion_check_pending
    org.workspace_auto_stop_timeout_minutes = 5
    org.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    workspace = run.scheduled_task.workspace
    workspace.last_activity_at = timezone.now() - timedelta(minutes=10)
    workspace.save(update_fields=["last_activity_at"])
    asyncio.run(service.reconcile_runs())
    stop.assert_not_called()
