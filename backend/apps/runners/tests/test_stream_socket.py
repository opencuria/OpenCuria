"""Ownership, chunk validation and ACKs for runner byte-stream replies."""

import uuid
from unittest.mock import AsyncMock

import pytest
import socketio

from apps.harness.access import runner_accessor
from apps.runners import sio_server
from apps.runners.services import RunnerService


@pytest.mark.django_db(transaction=True)
async def test_stream_service_validation(runner, workspace, monkeypatch):
    """Only valid chunks from the workspace's runner reach a live stream."""
    service = RunnerService()
    good = {
        "connection_id": "connection",
        "workspace_id": str(workspace.id),
        "stream": "stdout",
        "data": "aGk=",
    }
    assert not service.handle_stream_reply(
        "workspace:stream_output", good, runner_id=str(runner.id)
    )  # No live accessor owns this connection.

    routed = []
    monkeypatch.setattr(
        runner_accessor, "route_stream_output", lambda data: routed.append(data) or True
    )
    monkeypatch.setattr(
        runner_accessor, "route_stream_closed", lambda data: routed.append(data) or True
    )
    for event in ("workspace:stream_output", "workspace:stream_closed"):
        assert service.handle_stream_reply(event, good, runner_id=str(runner.id))
    assert routed == [good, good]
    routed.clear()

    for data, owner in (
        ({**good, "data": "!!!"}, str(runner.id)),
        ({**good, "data": ""}, str(runner.id)),
        (good, str(uuid.uuid4())),
        ({**good, "workspace_id": str(uuid.uuid4())}, str(runner.id)),
    ):
        assert not service.handle_stream_reply(
            "workspace:stream_output", data, runner_id=owner
        )
    assert not service.handle_stream_reply(
        "workspace:stream_bogus", good, runner_id=str(runner.id)
    )
    assert routed == []


@pytest.mark.django_db(transaction=True)
async def test_stream_output_ack_rejects_unknown_connection(
    runner, workspace, monkeypatch
):
    """A real service rejection becomes a NACK at the Socket.IO boundary."""
    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(server)
    server.get_session = AsyncMock(return_value={"runner_id": str(runner.id)})
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: RunnerService())
    result = await server.handlers["/"]["workspace:stream_output"](
        runner.sid,
        {
            "connection_id": "no-such-connection",
            "workspace_id": str(workspace.id),
            "stream": "stdout",
            "data": "aGk=",
        },
    )
    assert result == {"ok": False}
