import asyncio
import uuid
from datetime import datetime, time, timezone
from types import SimpleNamespace

import pytest
from apps.organizations.models import Organization
from apps.scheduled_tasks.models import ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskScheduler, ScheduledTaskService


class FakeRepository:
    def due(self, now):
        return []

    def create_manual_run(self, task, scheduled_for):
        return SimpleNamespace(
            id="run",
            scheduled_task=task,
            scheduled_for=scheduled_for,
            status=ScheduledTaskRun.Status.CLAIMED,
        )

    def update_run(self, run, **fields):
        return run


@pytest.mark.asyncio
async def test_scheduler_fails_duplicate_start_before_recovery(monkeypatch):
    events = []

    class FakeLease:
        async def acquire(self):
            events.append("acquire")
            return False

        async def release(self):
            events.append("release")

    monkeypatch.setattr("apps.scheduled_tasks.lease.ScheduledTaskLease", FakeLease)
    scheduler = ScheduledTaskScheduler(
        service=ScheduledTaskService(repository=FakeRepository())
    )
    with pytest.raises(RuntimeError, match="Another backend process"):
        await scheduler.start()
    assert events == ["acquire", "release"]
    assert scheduler._task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_fails", [False, True])
async def test_scheduler_recovers_under_lease_before_dispatch(
    monkeypatch, recovery_fails
):
    events = []

    class FakeLease:
        async def acquire(self):
            events.append("acquire")
            return True

        async def release(self):
            events.append("release")

    class FakeRecovery:
        def recover_backend_restart(self):
            events.append("recover")
            if recovery_fails:
                raise RuntimeError("recovery failed")
            return 0

    async def skip_due():
        events.append("skip_due")

    monkeypatch.setattr("apps.scheduled_tasks.lease.ScheduledTaskLease", FakeLease)
    service = ScheduledTaskService(repository=FakeRepository())
    service.skip_due_on_startup = skip_due
    scheduler = ScheduledTaskScheduler(service=service, recovery=FakeRecovery())
    if recovery_fails:
        with pytest.raises(RuntimeError, match="recovery failed"):
            await scheduler.start()
        assert events == ["acquire", "recover", "release"]
    else:
        await scheduler.start()
        try:
            assert events == ["acquire", "recover", "skip_due"]
            assert scheduler._task is not None
        finally:
            await scheduler.stop()
        assert events == ["acquire", "recover", "skip_due", "release"]
    assert scheduler._lease is None
    assert scheduler._task is None


@pytest.mark.asyncio
async def test_scheduler_launches_due_work_supervised_without_waiting(monkeypatch):
    due = datetime(2025, 1, 1, 9, tzinfo=timezone.utc)
    task = SimpleNamespace(
        id="task",
        next_run_at=due,
        local_time=time(9),
        timezone_name="UTC",
        recurrence="daily",
        weekdays=[],
        enabled=True,
    )
    repo = FakeRepository()
    repo.due = lambda now: [task]
    repo.advance = lambda task_id, *, expected, next_run_at: True
    repo.claim = lambda task, *, scheduled_for, next_run_at: SimpleNamespace(
        id="claim",
        status=ScheduledTaskRun.Status.CLAIMED,
        scheduled_for=scheduled_for,
        scheduled_task=task,
    )
    repo.runs_for_occurrence = lambda task_id, scheduled_for: None
    service = ScheduledTaskService(repository=repo)
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow_dispatch(task, scheduled_for, *, ledger=None):
        started.set()
        await gate.wait()

    service._dispatch_claimed = slow_dispatch
    scheduler = ScheduledTaskScheduler(service=service, interval=0.01)
    scheduler._supervise_launch = lambda task, scheduled_for, ledger: (
        scheduler._launch_tasks.add(
            asyncio.create_task(slow_dispatch(task, scheduled_for, ledger=ledger))
        )
    )
    await service.dispatch_due(now=due, on_claim=scheduler._supervise_launch)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert scheduler._launch_tasks
    gate.set()
    await asyncio.gather(*scheduler._launch_tasks)
