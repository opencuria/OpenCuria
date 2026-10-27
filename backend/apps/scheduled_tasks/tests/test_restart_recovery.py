import uuid
from datetime import datetime, timezone

import pytest

from apps.accounts.models import User
from apps.harness.models import (
    HarnessMessage,
    HarnessPart,
    HarnessSession,
    QuestionRequest,
)
from apps.harness.permissions.models import PermissionRequest
from apps.organizations.models import Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.recovery import ScheduledTaskRecoveryService


@pytest.fixture
def recovery_schedule(db):
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
    return ScheduledTask.objects.create(
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


@pytest.mark.django_db
def test_restart_marks_only_scheduled_run_interrupted_and_closes_its_chat(
    recovery_schedule,
):
    schedule = recovery_schedule
    workspace = schedule.workspace
    org = schedule.organization
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

    assert ScheduledTaskRecoveryService().recover_backend_restart() == 1
    run.refresh_from_db()
    session.refresh_from_db()
    assistant.refresh_from_db()
    unrelated.refresh_from_db()
    assert run.status == ScheduledTaskRun.Status.INTERRUPTED
    assert session.status == "idle"
    assert assistant.finish == "aborted"
    assert unrelated.status == "busy"


def make_session(schedule, **fields):
    return HarnessSession.objects.create(
        workspace=schedule.workspace,
        organization_id=schedule.organization_id,
        mode="build",
        agent_name="build",
        status="busy",
        **fields,
    )


def make_run(schedule, **fields):
    return ScheduledTaskRun.objects.create(
        scheduled_task=schedule,
        scheduled_for=datetime.now(timezone.utc),
        **fields,
    )


@pytest.mark.parametrize(
    ("finish", "error", "expected"),
    [
        ("stop", "", "succeeded"),
        ("error", "", "error"),
        ("stop", "Provider failed", "error"),
        ("aborted", "", "succeeded"),
    ],
)
def test_restart_preserves_completed_assistant_outcome(
    recovery_schedule,
    finish,
    error,
    expected,
):
    session = make_session(recovery_schedule)
    completed_at = datetime.now(timezone.utc)
    assistant = HarnessMessage.objects.create(
        session=session,
        role="assistant",
        finish=finish,
        error=error,
        completed_at=completed_at,
    )
    run = make_run(
        recovery_schedule,
        session=session,
        assistant_message=assistant,
        status="running",
        reason="original reason",
    )

    assert ScheduledTaskRecoveryService().recover_backend_restart() == 1

    run.refresh_from_db()
    session.refresh_from_db()
    assistant.refresh_from_db()
    assert run.status == expected
    assert run.finished_at == completed_at
    assert run.error == error
    assert run.reason == "original reason"
    assert session.status == "busy"
    assert assistant.finish == finish
    assert ScheduledTaskRecoveryService().recover_backend_restart() == 0


def test_restart_finds_first_assistant_and_preserves_followup_inputs(recovery_schedule):
    session = make_session(recovery_schedule)
    followup = HarnessMessage.objects.create(
        session=session,
        role="assistant",
        position=2,
    )
    assistant = HarnessMessage.objects.create(
        session=session,
        role="assistant",
        position=1,
    )
    run = make_run(recovery_schedule, session=session)
    requests = []
    for message in (assistant, followup):
        fields = {
            "organization_id": recovery_schedule.organization_id,
            "workspace_id": recovery_schedule.workspace_id,
            "session_id": session.id,
            "message_id": message.id,
        }
        requests.append(
            (
                PermissionRequest.objects.create(
                    **fields, tool="terminal", pattern="*"
                ),
                QuestionRequest.objects.create(**fields, questions=[]),
            )
        )

    assert ScheduledTaskRecoveryService().recover_backend_restart() == 1

    run.refresh_from_db()
    session.refresh_from_db()
    assistant.refresh_from_db()
    followup.refresh_from_db()
    assert run.assistant_message_id == assistant.id
    assert run.status == "interrupted"
    assert run.reason == "backend_restart"
    assert run.finished_at == assistant.completed_at
    assert run.error == assistant.error == "Backend restarted before the run completed"
    assert session.status == "busy"
    assert followup.completed_at is None
    for pair, expected in zip(requests, ("rejected", "pending"), strict=True):
        for request in pair:
            request.refresh_from_db()
            assert request.status == expected
            assert (request.resolved_at is not None) == (expected == "rejected")


@pytest.mark.parametrize("with_session", [True, False])
def test_restart_recovers_claim_without_an_assistant(recovery_schedule, with_session):
    session = make_session(recovery_schedule) if with_session else None
    run = make_run(recovery_schedule, session=session)

    assert ScheduledTaskRecoveryService().recover_backend_restart() == 1

    run.refresh_from_db()
    assert run.status == "interrupted"
    assert run.assistant_message_id is None
    assert run.finished_at is not None
    if session is not None:
        session.refresh_from_db()
        assert session.status == "idle"


def test_restart_rolls_back_ledger_and_harness_writes_on_failure(recovery_schedule):
    session = make_session(recovery_schedule)
    assistant = HarnessMessage.objects.create(session=session, role="assistant")
    run = make_run(
        recovery_schedule,
        session=session,
        assistant_message=assistant,
        status="running",
    )
    HarnessPart.objects.create(
        message=assistant,
        type="subtask",
        meta={"child_session_id": "invalid"},
    )

    with pytest.raises(ValueError):
        ScheduledTaskRecoveryService().recover_backend_restart()

    run.refresh_from_db()
    assistant.refresh_from_db()
    session.refresh_from_db()
    assert run.status == "running"
    assert run.finished_at is None
    assert assistant.completed_at is None
    assert assistant.error == ""
    assert session.status == "busy"


def test_restart_cleans_nested_subtasks_only_from_the_lost_turn(recovery_schedule):
    root = make_session(recovery_schedule)
    assistant = HarnessMessage.objects.create(
        session=root, role="assistant", position=0
    )
    followup = HarnessMessage.objects.create(session=root, role="assistant", position=1)
    make_run(
        recovery_schedule,
        session=root,
        assistant_message=assistant,
        status="running",
    )
    child = make_session(recovery_schedule, parent=root)
    grandchild = make_session(recovery_schedule, parent=child)
    other_child = make_session(recovery_schedule, parent=root)
    for position, (message, child_id) in enumerate(
        (
            (assistant, str(child.id)),
            (assistant, None),
            (assistant, ""),
            (assistant, str(uuid.uuid4())),
            (followup, str(other_child.id)),
        )
    ):
        HarnessPart.objects.create(
            message=message,
            type="subtask",
            meta={"child_session_id": child_id},
            position=position,
        )
    messages = [
        HarnessMessage.objects.create(session=session, role="assistant")
        for session in (child, grandchild, other_child)
    ]
    completed = HarnessMessage.objects.create(
        session=grandchild,
        role="assistant",
        position=1,
        finish="stop",
        completed_at=datetime.now(timezone.utc),
    )
    request = QuestionRequest.objects.create(
        organization_id=recovery_schedule.organization_id,
        workspace_id=recovery_schedule.workspace_id,
        session_id=grandchild.id,
        message_id=messages[1].id,
        questions=[],
    )

    assert ScheduledTaskRecoveryService().recover_backend_restart() == 1

    for session, message, interrupted in zip(
        (child, grandchild, other_child),
        messages,
        (True, True, False),
        strict=True,
    ):
        session.refresh_from_db()
        message.refresh_from_db()
        assert session.status == ("idle" if interrupted else "busy")
        assert (message.completed_at is not None) == interrupted
        if interrupted:
            assert message.finish == "aborted"
    completed.refresh_from_db()
    request.refresh_from_db()
    assert completed.finish == "stop"
    assert request.status == "rejected"
