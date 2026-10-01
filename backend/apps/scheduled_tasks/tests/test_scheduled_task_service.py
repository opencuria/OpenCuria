import asyncio
import uuid
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from apps.runners.enums import WorkspaceOperation
from apps.scheduled_tasks.models import ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskService
from common.exceptions import ConflictError


class FakeRepository:
    def __init__(self):
        self.rows = []
        self.advanced = []
        self.skipped = []
        self.busy = False
        self.status = "running"

    def due(self, now):
        return self.rows

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

    def workspace_status(self, workspace_id):
        return self.status

    def workspace_resume_state(self, workspace_id):
        return self.status, None, "online", True

    def workspace_is_owned(self, workspace_id, *, organization_id, owner_id):
        return True

    def has_busy_harness_session(self, workspace_id):
        return self.busy

    def touch_workspace_activity(self, workspace_id):
        return None

    def running_workspaces_with_autostop(self):
        return []

    def workspace_autostop_settings(self, workspace_id):
        return None

    def create_run(self, task, scheduled_for):
        return SimpleNamespace(
            id=uuid.uuid4(),
            scheduled_task=task,
            scheduled_for=scheduled_for,
            status=ScheduledTaskRun.Status.CLAIMED,
            configuration_snapshot={},
        )

    create_manual_run = create_run

    def lock_workspace_for_launch(self, workspace_id):
        return self.status, self.busy


@pytest.mark.asyncio
async def test_dispatch_skips_missed_occurrence_and_advances_without_catchup():
    repo = FakeRepository()
    due = datetime(2025, 1, 1, 9, tzinfo=timezone.utc)
    task = SimpleNamespace(
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
    repo.status = "stopped"

    class FakeRunner:
        async def resume_workspace(self, workspace_id):
            repo.status = "running"

    class FakeHarness:
        def create_session(self, **values):
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            assert session.parent_id is None if hasattr(session, "parent_id") else True
            return SimpleNamespace(id=uuid.uuid4())

        async def delete_session(self, session_id):
            return None

    task = SimpleNamespace(
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
    service = ScheduledTaskService(
        repository=repo, harness=FakeHarness(), runner=FakeRunner()
    )
    run = await service.run_now(task)
    assert run.status == ScheduledTaskRun.Status.RUNNING
    assert run.session is not None
    assert run.assistant_message is not None
    assert repo.status == "running"


@pytest.mark.asyncio
async def test_existing_starting_operation_waits_for_resume_confirmation(monkeypatch):
    repo = FakeRepository()
    repo.status = "stopped"
    resume_states = iter(
        [
            ("stopped", WorkspaceOperation.STARTING, "online", True),
            ("running", None, "online", True),
        ]
    )
    repo.workspace_resume_state = lambda workspace_id: next(resume_states)
    statuses = iter(["stopped", "running"])
    repo.workspace_status = lambda workspace_id: next(statuses)

    class UnexpectedRunner:
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
    service = ScheduledTaskService(
        repository=repo, runner=UnexpectedRunner(), harness=FakeHarness()
    )
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
    repo.status, operation, runner = state
    repo.workspace_resume_state = lambda workspace_id: (
        repo.status,
        operation,
        runner,
        True,
    )
    task = SimpleNamespace(
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

    service = ScheduledTaskService(repository=repo, harness=UnexpectedHarness())
    run = await service._launch(task, scheduled_for=task.next_run_at)
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "workspace_operation_active"


@pytest.mark.asyncio
async def test_resume_revalidates_state_before_start(monkeypatch):
    repo = FakeRepository()
    repo.status = "stopped"
    statuses = iter(["stopped", "running"])
    repo.workspace_status = lambda workspace_id: next(statuses)
    state_reads = iter(
        [
            ("stopped", WorkspaceOperation.STARTING, "online", True),
            ("running", WorkspaceOperation.STOPPING, "online", True),
        ]
    )
    repo.workspace_resume_state = lambda workspace_id: next(state_reads)
    class UnexpectedHarness:
        def create_session(self, **values):
            pytest.fail("state changed during resume; do not start a run")

    service = ScheduledTaskService(repository=repo, harness=UnexpectedHarness())

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    task = SimpleNamespace(
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
    repo = FakeRepository()
    repo.status = "resuming"
    statuses = iter(["resuming", "running"])
    repo.workspace_status = lambda workspace_id: next(statuses)
    service = ScheduledTaskService(repository=repo)

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


@pytest.mark.asyncio
async def test_started_run_survives_activity_touch_failure():
    repo = FakeRepository()
    repo.touch_workspace_activity = lambda workspace_id: (_ for _ in ()).throw(
        RuntimeError("database unavailable")
    )

    class FakeHarness:
        def create_session(self, **values):
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            return SimpleNamespace(id=uuid.uuid4())

        async def delete_session(self, session_id):
            pytest.fail("started session must not be deleted")

    task = SimpleNamespace(
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
    service = ScheduledTaskService(repository=repo, harness=FakeHarness())
    result = await service._launch(task, scheduled_for=run.scheduled_for, ledger=run)
    assert result.status == ScheduledTaskRun.Status.RUNNING
    assert result.assistant_message is not None


@pytest.mark.asyncio
async def test_resume_is_skipped_when_runner_offline():
    repo = FakeRepository()
    repo.status = "stopped"

    class RunnerOffline:
        async def resume_workspace(self, workspace_id):
            raise AssertionError("must not ask an offline runner to resume")

    task = SimpleNamespace(
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
    repo.workspace_resume_state = lambda workspace_id: (
        "stopped",
        None,
        "offline",
        True,
    )
    service = ScheduledTaskService(repository=repo, runner=RunnerOffline())
    run = await service.run_now(task)
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "workspace_resume_failed"


@pytest.mark.asyncio
async def test_second_manual_run_of_same_task_is_skipped():
    repo = FakeRepository()
    task = SimpleNamespace(
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

    service = ScheduledTaskService(repository=repo, harness=FakeHarness())
    first = await service.run_now(task)
    second = await service.run_now(task)
    assert first.status == ScheduledTaskRun.Status.RUNNING
    assert second.status == ScheduledTaskRun.Status.SKIPPED
    assert second.reason == "task_already_active"


@pytest.mark.asyncio
async def test_manual_run_skips_busy_workspace_without_changing_schedule():
    repo = FakeRepository()
    repo.busy = True
    scheduled = datetime(2025, 1, 4, 9, tzinfo=timezone.utc)
    task = SimpleNamespace(
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

    service = ScheduledTaskService(repository=repo, harness=FakeHarness())
    run = await service.run_now(task)
    assert run.status == ScheduledTaskRun.Status.SKIPPED
    assert run.reason == "other_chat_active"
    assert task.next_run_at == scheduled
