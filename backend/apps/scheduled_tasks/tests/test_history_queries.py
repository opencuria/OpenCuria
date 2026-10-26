"""History metadata stays independent of stored assistant answer sizes."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.harness.models import HarnessMessage, HarnessSession
from apps.mcp_app.tools.scheduled_tasks import _run as mcp_run
from apps.organizations.models import Organization
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.api import _run_out
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.repositories import ScheduledTaskRepository
from apps.scheduled_tasks.services import ScheduledTaskService


@pytest.fixture
def history_task(db: None) -> ScheduledTask:
    """An owned task with no provider or runner connection."""
    organization = Organization.objects.create(name="History", slug="history")
    owner = User.objects.create_user(email="history@example.com")
    runner = Runner.objects.create(
        organization=organization, api_token_hash=uuid.uuid4().hex
    )
    workspace = Workspace.objects.create(runner=runner, created_by=owner)
    return ScheduledTask.objects.create(
        organization=organization,
        owner=owner,
        workspace=workspace,
        name="Review",
        prompt="Review the source",
        recurrence="daily",
        local_time="09:00",
        next_run_at=timezone.now(),
    )


def _history(task: ScheduledTask, count: int) -> list[ScheduledTaskRun]:
    """Include large private bodies and both terminal assistant outcomes."""
    runs = []
    for index in range(count):
        session = HarnessSession.objects.create(
            workspace_id=task.workspace_id,
            organization_id=task.organization_id,
            engine_state={"fixture": "private session state"},
        )
        failed = index % 2 == 0
        completed_at = task.next_run_at + timedelta(days=index, minutes=1)
        message = HarnessMessage.objects.create(
            session=session,
            role="assistant",
            content="Private answer body. " * 4096,
            finish="error" if failed else "stop",
            error="Provider failed" if failed else "",
            completed_at=completed_at,
        )
        runs.append(
            ScheduledTaskRun.objects.create(
                scheduled_task=task,
                session=session,
                assistant_message=message,
                scheduled_for=task.next_run_at + timedelta(days=index),
                configuration_snapshot={"prompt": "Original scheduled prompt"},
                status="error" if failed else "succeeded",
                finished_at=completed_at,
                error=message.error,
            )
        )
    return runs


def _assert_metadata_reads(queries: CaptureQueriesContext, count: int) -> None:
    """Guard database work and transfer size, without timing-dependent assertions."""
    assert len(queries) == count
    for query in queries:
        assert '"harness_message"."content"' not in query["sql"]
        assert 'JOIN "harness_session"' not in query["sql"]
        assert 'JOIN "scheduled_tasks_task"' not in query["sql"]


@pytest.mark.parametrize("count", [0, 1, 101])
def test_history_metadata_is_bounded_and_renders_without_extra_queries(
    history_task: ScheduledTask, count: int
) -> None:
    runs = _history(history_task, count)
    expected = list(reversed(runs))[:100]
    with CaptureQueriesContext(connection) as queries:
        rows = ScheduledTaskService().list_runs(history_task)
        rest = [_run_out(row).model_dump(mode="json") for row in rows]
        mcp = [mcp_run(row) for row in rows]

    _assert_metadata_reads(queries, 2)
    assert [row["id"] for row in rest] == [str(run.id) for run in expected]
    assert [row["run_id"] for row in mcp] == [run.id for run in expected]
    for row, original in zip(rest, expected, strict=True):
        assert row["session_id"] == str(original.session_id)
        assert row["status"] == original.status
        assert row["assistant_finish"] == original.assistant_message.finish
        assert row["assistant_error"] == original.error
        assert row["configuration_snapshot"] == original.configuration_snapshot


def test_history_repairs_completed_runs_without_loading_their_answers(
    history_task: ScheduledTask,
) -> None:
    runs = _history(history_task, 3)
    ScheduledTaskRun.objects.filter(id__in=[run.id for run in runs]).update(
        status="running", error="", finished_at=None
    )
    HarnessMessage.objects.filter(id=runs[2].assistant_message_id).update(
        completed_at=None, finish="", error=""
    )
    with CaptureQueriesContext(connection) as queries:
        rows = ScheduledTaskService().list_runs(history_task)
        outcomes = {row.id: _run_out(row) for row in rows}
        for row in rows:
            mcp_run(row)

    # One repair read, two updates, one history read.
    _assert_metadata_reads(queries, 4)
    assert outcomes[runs[0].id].status == "error"
    assert outcomes[runs[0].id].error == "Provider failed"
    assert outcomes[runs[1].id].status == "succeeded"
    assert outcomes[runs[1].id].finished_at == runs[1].finished_at
    assert outcomes[runs[2].id].status == "running"
    assert outcomes[runs[2].id].finished_at is None
    assert ScheduledTaskRun.objects.filter(completion_check_pending=True).count() == 2


@pytest.mark.parametrize("unlink", ["message", "session"])
def test_history_handles_missing_message_and_session_links(
    history_task: ScheduledTask, unlink: str
) -> None:
    run = _history(history_task, 1)[0]
    if unlink == "session":
        HarnessSession.objects.filter(pk=run.session_id).delete()
    else:
        HarnessMessage.objects.filter(pk=run.assistant_message_id).delete()
    with CaptureQueriesContext(connection) as queries:
        row = ScheduledTaskService().list_runs(history_task)[0]
        rest = _run_out(row)
        mcp = mcp_run(row)

    _assert_metadata_reads(queries, 2)
    assert rest.session_id == (None if unlink == "session" else run.session_id)
    assert rest.status == "error"
    assert rest.assistant_finish == ""
    assert rest.assistant_error == ""
    assert rest.error == mcp["error"] == "Provider failed"


def test_active_outcomes_are_scoped_by_task_and_message(
    history_task: ScheduledTask,
) -> None:
    runs = _history(history_task, 2)
    ScheduledTaskRun.objects.filter(pk=runs[0].id).update(status="running")
    assert ScheduledTaskRepository.active_runs(task_id=uuid.uuid4()) == []
    assert (
        ScheduledTaskRepository.active_runs(message_id=runs[1].assistant_message_id)
        == []
    )
    with CaptureQueriesContext(connection) as queries:
        active = ScheduledTaskRepository.active_runs(
            task_id=history_task.id, message_id=runs[0].assistant_message_id
        )
        assert [(row.id, row.assistant_message.error) for row in active] == [
            (runs[0].id, "Provider failed")
        ]
    _assert_metadata_reads(queries, 1)
