import asyncio
import uuid
from datetime import datetime, time, timezone
from types import SimpleNamespace

import pytest
from django.test import override_settings
from apps.organizations.models import Organization
from apps.scheduled_tasks.models import ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskScheduler, ScheduledTaskService


class FakeRepository:
    def recover_backend_restart(self):
        return 0

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
        recovered_previous_owner = False
        _clean_handoff = True

        def __init__(self, *, deployment_version="", owner_id="local"):
            events.append(("init", deployment_version, owner_id))

        async def acquire(self):
            events.append("acquire")
            return False

        async def release(self):
            events.append("release")

    monkeypatch.setattr("apps.scheduled_tasks.lease.ScheduledTaskLease", FakeLease)
    scheduler = ScheduledTaskScheduler(
        service=ScheduledTaskService(repository=FakeRepository()),
        deployment_version="release-a",
    )
    with pytest.raises(RuntimeError, match="Another backend process"):
        await scheduler.start()
    assert events == [("init", "release-a", "local"), "acquire", "release"]
    assert scheduler._task is None


@pytest.mark.asyncio
async def test_managed_scheduler_does_not_recover_runs_after_clean_handoff(monkeypatch):
    events = []

    class FakeLease:
        recovered_previous_owner = False
        _clean_handoff = True

        def __init__(self, *, deployment_version="", owner_id="local"):
            events.append(("init", deployment_version, owner_id))

        async def acquire(self):
            return True

        async def release(self):
            events.append("release")

        async def renew(self):
            return True

    class Service:
        repository = FakeRepository()

        async def skip_due_on_startup(self):
            events.append("skip-due")

    monkeypatch.setattr("apps.scheduled_tasks.lease.ScheduledTaskLease", FakeLease)
    scheduler = ScheduledTaskScheduler(Service(), deployment_version="release-b")
    import apps.deployments.services as deployments
    from apps.deployments.services import ReleaseManifest

    manifest = ReleaseManifest(
        version="release-b",
        protocol_min=1,
        protocol_max=1,
        database_schema=1,
        database_schema_min=1,
        database_schema_max=1,
    )
    monkeypatch.setattr(deployments, "live_generation_manifests", lambda: (manifest,))
    monkeypatch.setattr(deployments, "require_compatible_releases", lambda *a, **kw: None)
    monkeypatch.setattr(
        "django.conf.settings.DEPLOYMENT_RELEASE",
        {
            "version": "release-b",
            "protocol_min": 1,
            "protocol_max": 1,
            "database_schema": 1,
            "database_schema_min": 1,
            "database_schema_max": 1,
        },
    )
    from django.test import override_settings

    with override_settings(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3"}}
    ):
        await scheduler.start()
        await scheduler.stop()
    assert events == [("init", "release-b", "local"), "skip-due", "release"]


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
