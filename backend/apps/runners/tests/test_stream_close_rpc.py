"""Stream control uses Socket.IO ACKs without a second reply-event path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from apps.runners.services import RunnerService


@pytest.mark.parametrize(
    "event",
    [
        "workspace:stream_start",
        "workspace:stream_input",
        "workspace:stream_close",
    ],
)
@pytest.mark.parametrize(
    "response",
    [
        {"ok": True, "closed": True},
        {"ok": True, "closed": False},
        {"ok": False, "closed": False, "error": "unverified"},
    ],
)
async def test_stream_control_preserves_ack(event: str, response: dict) -> None:
    """The runner's ACK is authoritative, including negative close evidence."""
    sio = SimpleNamespace(call=AsyncMock(return_value=response))
    service = RunnerService(sio_server=sio)
    payload = {"workspace_id": "workspace", "connection_id": "conn"}
    result = await service.call_stream_event(SimpleNamespace(sid="sid"), event, payload)
    assert result is response
    sio.call.assert_awaited_once_with(event, payload, to="sid", timeout=15)


async def test_stream_control_ack_timeout() -> None:
    """A missing ACK fails the call instead of waiting for another protocol."""
    sio = SimpleNamespace(call=AsyncMock(side_effect=SocketIOTimeoutError))
    service = RunnerService(sio_server=sio)
    with pytest.raises(RuntimeError, match="timed out.*workspace:stream_close"):
        await service.call_stream_event(
            SimpleNamespace(sid="sid"), "workspace:stream_close", {}, timeout=2
        )
    sio.call.assert_awaited_once_with("workspace:stream_close", {}, to="sid", timeout=2)
