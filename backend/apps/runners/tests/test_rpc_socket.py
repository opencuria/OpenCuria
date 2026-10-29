"""Authenticated Socket.IO routing for runner RPC and byte-stream replies."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import socketio

from apps.runners import sio_server
from apps.runners.enums import RunnerStatus
from apps.runners.services import RunnerService

REPLIES = [
    ("harness:exec_wait_result", "handle_harness_reply"),
    ("harness:read_file_result", "handle_harness_reply"),
    ("harness:read_file_chunk", "handle_harness_reply"),
    ("harness:write_file_result", "handle_harness_reply"),
    ("harness:list_result", "handle_harness_reply"),
    ("harness:desktop_action_result", "handle_harness_reply"),
    ("workspace:artifact_ensure_result", "handle_harness_reply"),
    ("git:operation_result", "handle_git_reply"),
    ("harness:process_start_result", "handle_process_reply"),
    ("harness:process_list_result", "handle_process_reply"),
    ("harness:process_get_result", "handle_process_reply"),
    ("harness:process_stop_result", "handle_process_reply"),
    ("harness:process_verify_result", "handle_process_reply"),
    ("workspace:stream_output", "handle_stream_reply"),
    ("workspace:stream_closed", "handle_stream_reply"),
]


@pytest.fixture
def rpc_socket(monkeypatch, runner):
    """Use real runner-session validation and replace only reply consumers."""
    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(server)
    server.emit = AsyncMock()
    server.get_session = AsyncMock(return_value={"runner_id": str(runner.id)})
    consumers = {method: Mock() for _, method in REPLIES}
    service = SimpleNamespace(
        is_current_runner_session=RunnerService().is_current_runner_session,
        **consumers,
    )
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    return server, consumers


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("event,method", REPLIES)
@pytest.mark.parametrize("accepted", [False, True])
async def test_rpc_reply_routing(
    rpc_socket, runner, workspace, event, method, accepted
):
    """Route exact payloads to one consumer; only byte streams ACK acceptance."""
    server, consumers = rpc_socket
    consumers[method].return_value = accepted
    data = {"request_id": "request", "workspace_id": str(workspace.id)}

    result = await server.handlers["/"][event](runner.sid, data)

    consumers[method].assert_called_once_with(event, data, runner_id=str(runner.id))
    for other, consumer in consumers.items():
        if other != method:
            consumer.assert_not_called()
    assert result == ({"ok": accepted} if method == "handle_stream_reply" else None)
    server.emit.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("event,method", REPLIES)
@pytest.mark.parametrize("rejected", ["unauthenticated", "stale_sid", "offline"])
async def test_rpc_reply_rejects_invalid_session(
    rpc_socket, runner, event, method, rejected
):
    """Every reply requires the current authenticated, online runner session."""
    server, consumers = rpc_socket
    sid = runner.sid
    if rejected == "unauthenticated":
        server.get_session.return_value = {}
    elif rejected == "stale_sid":
        sid = "previous-runner-connection"
    else:
        runner.status = RunnerStatus.OFFLINE
        runner.save(update_fields=["status"])

    result = await server.handlers["/"][event](sid, {"ok": True})

    for consumer in consumers.values():
        consumer.assert_not_called()
    assert result == (
        {"ok": False, "error": "unauthenticated"}
        if method == "handle_stream_reply"
        else None
    )
    server.emit.assert_not_awaited()
