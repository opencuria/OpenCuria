"""Explicit close outcomes isolate requests and preserve false evidence."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from apps.runners.services.domains.stream_transport import StreamTransportMixin
from apps.runners.services.infra.rpc_registry import RpcRegistryMixin
from apps.runners.services.infra.runner_transport import RunnerTransportMixin


class Transport(RpcRegistryMixin, RunnerTransportMixin, StreamTransportMixin):
    def __init__(self):
        self._call_pending = {}
        self.sio = SimpleNamespace(call=AsyncMock())
        self.workspace_id = str(uuid.uuid4())
        self.workspaces = SimpleNamespace(get_runner_id=lambda _: "runner")

    def _validate_harness_workspace_runner(self, workspace_id, runner_id):
        return str(workspace_id) == self.workspace_id and runner_id == "runner"

    def reply(self, payload, **overrides):
        return self.handle_stream_reply(
            "workspace:stream_close_result",
            {**payload, "ok": True, "closed": False, **overrides},
            runner_id="runner",
        )

    async def close(self):
        return await self._call_runner(
            SimpleNamespace(sid="sid"),
            "workspace:stream_close",
            {"workspace_id": self.workspace_id, "connection_id": "conn"},
            timeout=0.02,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [True, False])
async def test_reply_before_ack_timeout_preserves_false(ok):
    service = Transport()

    async def no_ack(event, data, **kwargs):
        assert service.reply(data, ok=ok)
        raise SocketIOTimeoutError()

    service.sio.call.side_effect = no_ack
    result = await service.close()
    assert result["ok"] is ok
    assert result["closed"] is False
    assert service._call_pending == {}


@pytest.mark.asyncio
async def test_concurrent_and_delayed_retry_auth():
    service = Transport()
    sent = []

    async def no_ack(event, data, **kwargs):
        sent.append(data)
        raise SocketIOTimeoutError()

    service.sio.call.side_effect = no_ack
    with pytest.raises(RuntimeError, match="timed out"):
        await service.close()
    tasks = [asyncio.create_task(service.close()) for _ in range(2)]
    await asyncio.sleep(0)
    assert len({data["close_request_id"] for data in sent}) == 3
    assert not service.reply(sent[0], closed=True)
    assert not service.reply(sent[1], workspace_id=str(uuid.uuid4()), closed=True)
    assert not service.handle_stream_reply(
        "workspace:stream_close_result",
        {**sent[1], "ok": True, "closed": True},
        runner_id="foreign",
    )
    assert not tasks[0].done()
    assert service.reply(sent[2], closed=True)
    assert service.reply(sent[1], ok=False, closed=False)
    results = await asyncio.gather(*tasks)
    assert results[0]["ok"] is False
    assert results[0]["closed"] is False
    assert results[1]["closed"] is True
