"""Managed stream identities and confirmed cleanup across the accessor boundary."""

from __future__ import annotations

import asyncio

import pytest

from apps.harness.access.base import StreamClosedError
from apps.harness.access.runner_accessor import RunnerWorkspaceAccessor


class ManagedTransport:
    """Controllable runner replies, including retryable close uncertainty."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.close_result: dict = {"ok": True, "closed": True}
        self.close_started = asyncio.Event()
        self.close_gate: asyncio.Event | None = None

    async def emit(self, event: str, payload: dict) -> None:
        self.calls.append((event, payload))

    async def call(self, event: str, payload: dict, timeout=None) -> dict:
        self.calls.append((event, payload))
        if event == "workspace:stream_close":
            self.close_started.set()
            if self.close_gate is not None:
                await self.close_gate.wait()
            return dict(self.close_result)
        return {"ok": True, **payload}


def accessor(transport: ManagedTransport) -> RunnerWorkspaceAccessor:
    return RunnerWorkspaceAccessor(
        "workspace", emit=transport.emit, call=transport.call
    )


async def test_managed_owner_reaches_runner_payload() -> None:
    transport = ManagedTransport()
    client = accessor(transport)
    owner = {"lease_id": "lease", "epoch": "epoch"}
    stream = await client.open_process(["mcp"], owner=owner)
    assert transport.calls[0][1]["owner"] == owner
    assert client._byte_streams[stream.connection_id].managed
    await stream.aclose()
    assert stream.connection_id not in client._byte_streams


@pytest.mark.parametrize(
    "result", [{"ok": True, "closed": False}, {"closed": True}, {"ok": False}]
)
async def test_uncertain_managed_close_preserves_retry_identity(result: dict) -> None:
    transport = ManagedTransport()
    client = accessor(transport)
    stream = await client.open_process(
        ["mcp"], owner={"lease_id": "lease", "epoch": "epoch"}
    )
    transport.close_result = result
    with pytest.raises(StreamClosedError, match="not confirmed"):
        await stream.aclose()
    assert stream.connection_id in client._byte_streams
    assert not stream._closed_locally
    transport.close_result = {"ok": True, "closed": True}
    await stream.aclose()
    assert stream.connection_id not in client._byte_streams


async def test_cancelled_close_joins_rpc_before_propagating() -> None:
    transport = ManagedTransport()
    client = accessor(transport)
    stream = await client.open_process(
        ["mcp"], owner={"lease_id": "lease", "epoch": "epoch"}
    )
    transport.close_gate = asyncio.Event()
    closing = asyncio.create_task(stream.aclose())
    await transport.close_started.wait()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    transport.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    # A cancelled caller may retry; confirmed identity remains available until then.
    await stream.aclose()
    assert stream.connection_id not in client._byte_streams


async def test_concurrent_close_is_idempotent() -> None:
    transport = ManagedTransport()
    client = accessor(transport)
    stream = await client.open_process(
        ["mcp"], owner={"lease_id": "lease", "epoch": "epoch"}
    )
    await asyncio.gather(stream.aclose(), stream.aclose())
    assert sum(event == "workspace:stream_close" for event, _ in transport.calls) == 1
