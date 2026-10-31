import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import asgi


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_fails", [False, True])
async def test_asgi_lifespan_starts_and_stops_single_scheduler(
    monkeypatch, startup_fails
):
    events = []

    class FakeScheduler:
        async def start(self, *, before_dispatch):
            events.append("started")
            await before_dispatch()

        async def stop(self):
            events.append("stopped")

    class FakeRecovery:
        async def start(self):
            events.append("recovery_started")

        async def stop(self):
            events.append("recovery_stopped")

    class FakeNotifications:
        async def start(self):
            if startup_fails:
                raise RuntimeError("notification startup failed")

        async def stop(self):
            pass

    monkeypatch.setattr(
        "apps.scheduled_tasks.services.ScheduledTaskScheduler", FakeScheduler
    )
    monkeypatch.setattr(
        "apps.harness.recovery_worker.HarnessRecoveryWorker", FakeRecovery
    )
    monkeypatch.setattr(
        "apps.notifications.worker.NotificationWorker", FakeNotifications
    )
    guard = SimpleNamespace(release_failed_start=Mock())
    monkeypatch.setattr(
        "common.process_ownership.acquire_backend_process_guard", lambda: guard
    )
    incoming = asyncio.Queue()
    outgoing = []
    await incoming.put({"type": "lifespan.startup"})
    await incoming.put({"type": "lifespan.shutdown"})

    async def receive():
        return await incoming.get()

    async def send(message):
        outgoing.append(message)

    await asgi.application(
        {"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send
    )
    assert events == ["started", "recovery_started", "recovery_stopped", "stopped"]
    guard.release_failed_start.assert_not_called()
    assert [message["type"] for message in outgoing] == (
        ["lifespan.startup.failed"]
        if startup_fails
        else ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    )
