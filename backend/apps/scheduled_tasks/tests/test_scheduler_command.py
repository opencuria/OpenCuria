"""Standalone scheduler role validation and graceful shutdown tests."""

from __future__ import annotations

import pytest
from django.core.management.base import CommandError

from apps.scheduled_tasks.management.commands.run_scheduled_task_scheduler import (
    Command,
)


def _release_settings() -> dict[str, object]:
    return {
        "version": "release-a",
        "protocol_min": 1,
        "protocol_max": 1,
        "database_schema": 1,
        "database_schema_min": 1,
        "database_schema_max": 1,
    }


def test_scheduler_command_rejects_worker_role(monkeypatch) -> None:
    from django.conf import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_RELEASE", _release_settings())
    monkeypatch.setenv("OPENCURIA_HARNESS_MODE", "worker")
    monkeypatch.setenv("OPENCURIA_DEPLOYMENT_VERSION", "release-a")
    monkeypatch.setenv("OPENCURIA_WORKER_ID", "worker-a")
    with pytest.raises(CommandError, match="gateway/local"):
        Command().handle(interval=1.0)


def test_scheduler_command_runs_lease_owned_scheduler_to_signal(monkeypatch) -> None:
    events: list[str] = []

    class FakeScheduler:
        def __init__(self, service, *, interval, deployment_version):
            events.append(f"init:{deployment_version}:{interval}")

        async def start(self):
            events.append("scheduler-start")

        async def stop(self):
            events.append("scheduler-stop")

    from django.conf import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_RELEASE", _release_settings())
    monkeypatch.setenv("OPENCURIA_HARNESS_MODE", "gateway")
    monkeypatch.setenv("OPENCURIA_DEPLOYMENT_VERSION", "release-a")
    monkeypatch.setattr(
        "apps.scheduled_tasks.management.commands.run_scheduled_task_scheduler.ScheduledTaskScheduler",
        FakeScheduler,
    )
    monkeypatch.setattr(
        "apps.scheduled_tasks.management.commands.run_scheduled_task_scheduler.get_harness_service",
        lambda: object(),
    )
    monkeypatch.setattr(
        Command,
        "_serve",
        lambda self, scheduler, deployment: fake_serve(
            scheduler, deployment, events
        ),
    )
    Command().handle(interval=1.0)
    assert events == [
        "init:release-a:1.0",
        "start",
        "scheduler-start",
        "scheduler-stop",
        "stop",
    ]


async def fake_serve(scheduler, deployment, events):
    """Exercise startup/shutdown ordering without a process signal wait."""
    assert deployment.version == "release-a"
    events.append("start")
    await scheduler.start()
    await scheduler.stop()
    events.append("stop")
