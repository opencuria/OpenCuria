import asyncio
import uuid
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from apps.runners.enums import WorkspaceOperation
from apps.scheduled_tasks.models import ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskService
from common.exceptions import ConflictError


class FakeRepository:
    def run_number(self, run):
        return 1

    def __init__(self):
        self.rows = []
        self.advanced = []
        self.skipped = []

    def due(self, now):
        return self.rows

    def active_runs(self, *, task_id=None, message_id=None):
        return []

    def claim(self, task, *, scheduled_for):
        return SimpleNamespace(
            id=uuid.uuid4(),
            scheduled_task=task,
            scheduled_for=scheduled_for,
            status="claimed",
        )

    def advance(self, task_id, *, expected, next_run_at):
        self.advanced.append(next_run_at)
        return True

    def mark_missed(self, task, *, scheduled_for, next_run_at):
        self.advanced.append(next_run_at)
        self.skipped.append("missed_occurrence")

    def update_run(self, run, **fields):
        for key, value in fields.items():
            setattr(run, key, value)
        if fields.get("status") == "skipped":
            self.skipped.append(run.reason)
        return run

    def update(self, task, **fields):
        for key, value in fields.items():
            setattr(task, key, value)
        return task

    def release_unstarted_claim(self, run_id, session_id, *, task_id):
        return True

    def create_run(self, task, scheduled_for):
        return SimpleNamespace(
            id=uuid.uuid4(),
            scheduled_task=task,
            scheduled_for=scheduled_for,
            status=ScheduledTaskRun.Status.CLAIMED,
            configuration_snapshot={},
        )

    create_manual_run = create_run


class FakeRunner:
    """Runner service double for the workspace facts a launch reads."""

    def __init__(self, status="running"):
        self.status = status

    def get_workspace_launch_state(self, workspace_id):
        return self.status, None, "online", True, None

    def is_active_workspace_owner(self, user_id, workspace_id, *, organization_id):
        return True

    async def touch_workspace_activity(self, workspace_id):
        return None


class FakeConversations:
    def discard_unstarted(self, session_id, *, workspace_id, organization_id, release):
        return release()


def make_service(repo, *, runner=None, harness=None):
    return ScheduledTaskService(
        repository=repo,
        runner=runner or FakeRunner(),
        harness=harness,
        conversations=FakeConversations(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", ["workspace became busy", "capturing image"])
async def test_auto_stop_reconciles_via_runner_auto_stop_guard_and_ignores_conflict(
    diagnostic,
):
    workspace_id = uuid.uuid4()

    class AutoStopRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def auto_stop_if_inactive(self, workspace_id):
            self.calls.append(workspace_id)
            raise ConflictError(diagnostic)

    runner = AutoStopRunner()
    service = make_service(FakeRepository(), runner=runner)

    await service._maybe_stop_workspace(workspace_id)

    assert runner.calls == [workspace_id]


@pytest.mark.asyncio
async def test_dispatch_skips_missed_occurrence_and_advances_without_catchup():
    repo = FakeRepository()
    due = datetime(2025, 1, 1, 9, tzinfo=timezone.utc)
    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        next_run_at=due,
        local_time=time(9),
        timezone_name="UTC",
        recurrence="daily",
        weekdays=[],
        enabled=True,
    )
    repo.rows = [task]
    service = ScheduledTaskService(repository=repo)
    await service.dispatch_due(now=datetime(2025, 1, 3, 12, tzinfo=timezone.utc))
    assert repo.advanced == [datetime(2025, 1, 4, 9, tzinfo=timezone.utc)]
    assert repo.skipped == ["missed_occurrence"]


@pytest.mark.asyncio
async def test_stopped_workspace_resumes_then_starts_a_new_root_session():
    repo = FakeRepository()

    class ResumingRunner(FakeRunner):
        async def resume_workspace(self, workspace_id):
            self.status = "running"

    class FakeHarness:
        def create_session(self, **values):
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            assert session.parent_id is None if hasattr(session, "parent_id") else True
            return SimpleNamespace(id=uuid.uuid4())

        async def delete_session(self, session_id):
            return None

    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime(2025, 1, 4, 9, tzinfo=timezone.utc),
        prompt="Review changes",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )
    runner = ResumingRunner(status="stopped")
    service = make_service(repo, harness=FakeHarness(), runner=runner)
    run = await service.run_now(task)
    assert run.status == ScheduledTaskRun.Status.RUNNING
    assert run.session is not None
    assert run.assistant_message is not None
    assert runner.status == "running"


@pytest.mark.asyncio
async def test_heartbeat_running_status_does_not_end_own_resume_wait(monkeypatch):
    """A heartbeat may report running before the runner confirms the resume."""
    repo = FakeRepository()
    state = {"value": ("stopped", None, "online", True, None)}

    class ResumingRunner(FakeRunner):
        def get_workspace_launch_state(self, workspace_id):
            return state["value"]

        async def resume_workspace(self, workspace_id):
            # The resume operation is allocated, then a heartbeat sees the VM.
            state["value"] = (
                "running",
                WorkspaceOperation.STARTING,
                "online",
                True,
                uuid.uuid4(),
            )

    class FakeHarness:
        def create_session(self, **values):
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            return SimpleNamespace(id=uuid.uuid4())

    async def runner_confirms_resume(_):
        state["value"] = ("running", None, "online", True, None)

    monkeypatch.setattr(asyncio, "sleep", runner_confirms_resume)
    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime.now(timezone.utc),
        prompt="Review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )
    service = make_service(repo, harness=FakeHarness(), runner=ResumingRunner())
    run = await service._launch(task, scheduled_for=task.next_run_at)
    assert run.status == ScheduledTaskRun.Status.RUNNING
    assert run.assistant_message is not None


@pytest.mark.asyncio
async def test_existing_starting_operation_waits_for_resume_confirmation(monkeypatch):
    repo = FakeRepository()
    resume_states = iter(
        [
            ("stopped", WorkspaceOperation.STARTING, "online", True, None),
            ("stopped", WorkspaceOperation.STARTING, "online", True, None),
            ("running", None, "online", True, None),
            ("running", None, "online", True, None),
        ]
    )

    class UnexpectedRunner(FakeRunner):
        def get_workspace_launch_state(self, workspace_id):
            return next(resume_states)

        async def resume_workspace(self, workspace_id):
            pytest.fail("an existing resume must not be dispatched again")

    class FakeHarness:
        def create_session(self, **values):
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            return SimpleNamespace(id=uuid.uuid4())

        async def delete_session(self, session_id):
            return None

    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime(2025, 1, 4, 9, tzinfo=timezone.utc),
        prompt="Review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )
    service = make_service(repo, runner=UnexpectedRunner(), harness=FakeHarness())

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    run = await service._launch(task, scheduled_for=task.next_run_at)
    assert run.status == ScheduledTaskRun.Status.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        ("running", None, "offline"),
        ("running", WorkspaceOperation.STOPPING, "online"),
    ],
)
async def test_running_workspace_is_skipped_when_runner_unavailable_or_busy(state):
    repo = FakeRepository()
    status, operation, runner_status = state
    runner = FakeRunner()
    runner.get_workspace_launch_state = lambda workspace_id: (
        status,
        operation,
        runner_status,
        True,
        None,
    )
    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime.now(timezone.utc),
        prompt="Review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )

    class UnexpectedHarness:
        def create_session(self, **values):
            pytest.fail("an unsafe workspace cannot start a harness run")

    service = make_service(repo, runner=runner, harness=UnexpectedHarness())
    run = await service._launch(task, scheduled_for=task.next_run_at)
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "workspace_operation_active"


@pytest.mark.asyncio
async def test_resume_revalidates_state_before_start(monkeypatch):
    repo = FakeRepository()
    state_reads = iter(
        [
            ("stopped", WorkspaceOperation.STARTING, "online", True, None),
            ("running", None, "online", True, None),
            ("running", WorkspaceOperation.STOPPING, "online", True, None),
        ]
    )
    runner = FakeRunner()
    runner.get_workspace_launch_state = lambda workspace_id: next(state_reads)

    class UnexpectedHarness:
        def create_session(self, **values):
            pytest.fail("state changed during resume; do not start a run")

    service = make_service(repo, runner=runner, harness=UnexpectedHarness())

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime.now(timezone.utc),
        prompt="Review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )
    run = await service._launch(task, scheduled_for=task.next_run_at)
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "workspace_operation_active"


@pytest.mark.asyncio
async def test_resume_waits_when_status_is_resuming(monkeypatch):
    states = iter(
        [
            ("resuming", WorkspaceOperation.STARTING, "online", True, None),
            ("running", None, "online", True, None),
        ]
    )
    runner = FakeRunner()
    runner.get_workspace_launch_state = lambda workspace_id: next(states)
    service = make_service(FakeRepository(), runner=runner)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    assert await service._wait_for_workspace_running(uuid.uuid4(), timeout_seconds=1)


@pytest.mark.asyncio
async def test_cancelled_launch_is_interrupted_not_left_claimed():
    repo = FakeRepository()
    run = repo.create_run(SimpleNamespace(id=uuid.uuid4()), datetime.now(timezone.utc))
    service = ScheduledTaskService(repository=repo)
    started = asyncio.Event()

    async def launch(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    service._launch = launch
    task = asyncio.create_task(
        service._dispatch_claimed(run.scheduled_task, run.scheduled_for, ledger=run)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert run.status == ScheduledTaskRun.Status.INTERRUPTED
    assert run.reason == "scheduler_stopped"


@pytest.mark.asyncio
async def test_manual_launch_cancellation_is_interrupted_not_left_claimed():
    repo = FakeRepository()
    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime.now(timezone.utc),
    )
    service = ScheduledTaskService(repository=repo)
    run = repo.create_run(task, task.next_run_at)
    repo.create_manual_run = lambda *_: run
    started = asyncio.Event()

    async def launch(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    service._launch = launch
    call = asyncio.create_task(service.run_now(task))
    await started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert run.status == ScheduledTaskRun.Status.INTERRUPTED


@pytest.mark.asyncio
async def test_editing_task_does_not_move_imminent_occurrence():
    repo = FakeRepository()
    due = datetime.now(timezone.utc) + timedelta(minutes=1)
    task = SimpleNamespace(
        name="Daily review",
        prompt="Old prompt",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
        recurrence="daily",
        weekdays=[],
        local_time=time(9),
        timezone_name="UTC",
        enabled=True,
        next_run_at=due,
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
    )
    service = ScheduledTaskService(repository=repo)
    updated = service.update(task, {"prompt": "New prompt", "name": "Renamed"})
    assert updated.next_run_at == due


def test_update_can_move_task_to_another_workspace(monkeypatch):
    repo = FakeRepository()
    old_workspace_id = uuid.uuid4()
    task = SimpleNamespace(
        name="Daily review",
        prompt="Prompt",
        mode="build",
        model="",
        reasoning_effort="",
        harness_id="native",
        skill_ids=[],
        recurrence="daily",
        weekdays=[],
        local_time=time(9),
        timezone_name="UTC",
        enabled=True,
        next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),
        workspace_id=old_workspace_id,
        organization_id=uuid.uuid4(),
        owner_id=1,
    )
    new_workspace = SimpleNamespace(id=uuid.uuid4())
    validated = []
    service = ScheduledTaskService(repository=repo)

    def fake_validate(**kwargs):
        validated.append(kwargs["workspace_id"])
        return "native", "", "", []

    monkeypatch.setattr(service, "_validate_engine_config", fake_validate)
    updated = service.update(task, {}, workspace=new_workspace)

    assert updated.workspace is new_workspace
    assert validated == [new_workspace.id]


@pytest.mark.asyncio
async def test_started_run_survives_activity_touch_failure():
    repo = FakeRepository()

    class UnavailableRunner(FakeRunner):
        async def touch_workspace_activity(self, workspace_id):
            raise RuntimeError("database unavailable")

    class FakeHarness:
        def create_session(self, **values):
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            return SimpleNamespace(id=uuid.uuid4())

        async def delete_session(self, session_id):
            pytest.fail("started session must not be deleted")

    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        prompt="Review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )
    run = repo.create_run(task, datetime.now(timezone.utc))
    service = make_service(repo, runner=UnavailableRunner(), harness=FakeHarness())
    result = await service._launch(task, scheduled_for=run.scheduled_for, ledger=run)
    assert result.status == ScheduledTaskRun.Status.RUNNING
    assert result.assistant_message is not None


@pytest.mark.asyncio
async def test_resume_is_skipped_when_runner_offline():
    repo = FakeRepository()

    class RunnerOffline(FakeRunner):
        def get_workspace_launch_state(self, workspace_id):
            return "stopped", None, "offline", True, None

        async def resume_workspace(self, workspace_id):
            raise AssertionError("must not ask an offline runner to resume")

    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime(2025, 1, 4, 9, tzinfo=timezone.utc),
        prompt="Review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )
    service = make_service(repo, runner=RunnerOffline())
    run = await service.run_now(task)
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "workspace_resume_failed"


@pytest.mark.asyncio
async def test_second_manual_run_of_same_task_is_skipped():
    repo = FakeRepository()
    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime(2025, 1, 4, 9, tzinfo=timezone.utc),
        prompt="Review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )
    active = False

    def create_manual_run(task, scheduled_for):
        nonlocal active
        if active:
            return SimpleNamespace(
                id=uuid.uuid4(),
                scheduled_task=task,
                scheduled_for=scheduled_for,
                status=ScheduledTaskRun.Status.SKIPPED,
                reason="task_already_active",
            )
        active = True
        return repo.create_run(task, scheduled_for)

    repo.create_manual_run = create_manual_run

    class FakeHarness:
        def create_session(self, **values):
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            return SimpleNamespace(id=uuid.uuid4())

        async def delete_session(self, session_id):
            return None

    service = make_service(repo, harness=FakeHarness())
    first = await service.run_now(task)
    second = await service.run_now(task)
    assert first.status == ScheduledTaskRun.Status.RUNNING
    assert second.status == ScheduledTaskRun.Status.SKIPPED
    assert second.reason == "task_already_active"


@pytest.mark.asyncio
async def test_manual_run_skips_busy_workspace_without_changing_schedule():
    repo = FakeRepository()
    scheduled = datetime(2025, 1, 4, 9, tzinfo=timezone.utc)
    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=scheduled,
        prompt="Review changes",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )

    class FakeHarness:
        def create_session(self, **values):
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            raise ConflictError("Another chat is already active in this workspace")

        async def delete_session(self, session_id):
            return None

    service = make_service(repo, harness=FakeHarness())
    run = await service.run_now(task)
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "other_chat_active"
    assert task.next_run_at == scheduled


def lifecycle_task():
    return SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        owner_id=1,
        next_run_at=datetime.now(timezone.utc),
        prompt="Review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "stopped"])
async def test_intervention_fence_skips_before_chat_creation_or_resume(status):
    class UnexpectedHarness:
        def create_session(self, **values):
            pytest.fail("intervention must block chat creation")

    class UnexpectedRunner(FakeRunner):
        def get_workspace_launch_state(self, workspace_id):
            return status, None, "online", True, uuid.uuid4()

        async def resume_workspace(self, workspace_id):
            pytest.fail("intervention must block resume")

    service = make_service(
        FakeRepository(), harness=UnexpectedHarness(), runner=UnexpectedRunner()
    )
    run = await service.run_now(lifecycle_task())
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "workspace_lifecycle_unresolved"
    assert "intervention required" in run.error


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("admission", ["create", "start"])
@pytest.mark.parametrize(
    "operation,current_task,reason",
    [
        (
            WorkspaceOperation.CAPTURING_IMAGE,
            uuid.uuid4(),
            "workspace_operation_active",
        ),
        (None, uuid.uuid4(), "workspace_lifecycle_unresolved"),
        (None, None, "other_chat_active"),
    ],
)
async def test_admission_conflict_uses_fresh_lifecycle_state(
    manual, admission, operation, current_task, reason
):
    repo = FakeRepository()
    runner = FakeRunner()
    task = lifecycle_task()
    cleaned = []

    def conflict():
        runner.get_workspace_launch_state = lambda _: (
            "running",
            operation,
            "online",
            True,
            current_task,
        )
        # Deliberately unrelated wording: persisted state determines the reason.
        raise ConflictError("Admission denied")

    async def notify(workspace_id):
        cleaned.append(workspace_id)

    class Harness:
        conversation_events = SimpleNamespace(emit=notify)

        def create_session(self, **values):
            if admission == "create":
                conflict()
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            conflict()

    service = make_service(repo, runner=runner, harness=Harness())
    if manual:
        run = await service.run_now(task)
    else:
        run = repo.create_run(task, task.next_run_at)
        await service._dispatch_claimed(task, task.next_run_at, ledger=run)
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == reason
    assert run.error == "Admission denied"
    assert len(cleaned) == (admission == "start")
    if admission == "start":
        assert run.session is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2025, 1, 1, 9, 0, 30, tzinfo=timezone.utc), "claimed"),
        (datetime(2025, 1, 3, 12, tzinfo=timezone.utc), "missed_occurrence"),
    ],
)
async def test_one_time_task_claims_or_misses_its_only_occurrence(now, expected):
    due = datetime(2025, 1, 1, 9, tzinfo=timezone.utc)
    task = SimpleNamespace(
        name="Daily review",
        id=uuid.uuid4(),
        next_run_at=due,
        local_time=time(9),
        timezone_name="UTC",
        recurrence="once",
        run_date=date(2025, 1, 1),
        weekdays=[],
        enabled=True,
    )
    repo = FakeRepository()
    repo.rows = [task]
    outcomes = []

    def claim(task, *, scheduled_for, next_run_at):
        outcomes.append(("claimed", next_run_at))
        return SimpleNamespace(status="claimed")

    def mark_missed(task, *, scheduled_for, next_run_at):
        outcomes.append(("missed_occurrence", next_run_at))

    repo.claim = claim
    repo.mark_missed = mark_missed
    service = ScheduledTaskService(repository=repo)
    await service.dispatch_due(now=now, on_claim=lambda *_args: None)
    # No following occurrence: the repository disables the finished task.
    assert outcomes == [(expected, None)]
