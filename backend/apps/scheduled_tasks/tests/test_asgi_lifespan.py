"""API restarts own transport only; execution and jobs have another lifetime."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from config import asgi


@pytest.mark.parametrize("startup_fails", [False, True])
async def test_asgi_lifespan_owns_only_reply_transport(monkeypatch, startup_fails):
    from apps.runners import reply_routing as runner_reply_relay

    start = AsyncMock(
        side_effect=RuntimeError("transport unavailable") if startup_fails else None
    )
    stop = AsyncMock()
    monkeypatch.setattr(runner_reply_relay, "start_runner_reply_relay", start)
    monkeypatch.setattr(runner_reply_relay, "stop_runner_reply_relay", stop)
    scheduler = Mock(side_effect=AssertionError("API must not construct a scheduler"))
    recovery = Mock(
        side_effect=AssertionError("API must not recover native executions")
    )
    notifications = Mock(side_effect=AssertionError("API must not own background jobs"))
    monkeypatch.setattr(
        "apps.scheduled_tasks.services.ScheduledTaskScheduler", scheduler
    )
    monkeypatch.setattr("apps.harness.recovery_worker.HarnessRecoveryWorker", recovery)
    monkeypatch.setattr("apps.notifications.worker.NotificationWorker", notifications)
    incoming = asyncio.Queue()
    outgoing = []
    await incoming.put({"type": "lifespan.startup"})
    await incoming.put({"type": "lifespan.shutdown"})

    async def send(message):
        outgoing.append(message)

    await asgi.application({"type": "lifespan"}, incoming.get, send)
    start.assert_awaited_once()
    stop.assert_awaited_once()
    scheduler.assert_not_called()
    recovery.assert_not_called()
    notifications.assert_not_called()
    assert [message["type"] for message in outgoing] == (
        ["lifespan.startup.failed"]
        if startup_fails
        else ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    )
