import asyncio

import pytest

from config import asgi


@pytest.mark.asyncio
async def test_asgi_lifespan_starts_and_stops_single_scheduler(monkeypatch):
    events = []

    class FakeScheduler:
        async def start(self):
            events.append("started")

        async def stop(self):
            events.append("stopped")

    monkeypatch.setattr(
        "apps.scheduled_tasks.services.ScheduledTaskScheduler", FakeScheduler
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
    assert events == ["started", "stopped"]
    assert [message["type"] for message in outgoing] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
