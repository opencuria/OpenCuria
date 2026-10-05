"""Managed desktop ownership and cancellation fences."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from apps.harness.desktop_leases import DesktopLease
from apps.harness.mcp_client.connection import McpServerConnection


class Accessor:
    def __init__(self):
        self.calls = []

    async def desktop_action(self, action, args):
        self.calls.append((action, args))
        return {
            "ok": True,
            "epoch": "epoch",
            "display": ":1",
            "xauthority": "/root/.Xauthority",
            "lease_state": {
                "reserve": "reserved",
                "hold": "held",
                "renew": "held",
                "release": "released",
            }.get(action),
        }


@pytest.mark.asyncio
async def test_reserve_idle_and_fresh_ownership():
    accessor = Accessor()
    lease = DesktopLease(accessor, kind="mcp")
    other = DesktopLease(accessor, kind="mcp")
    assert lease.owner_id != other.owner_id
    assert lease.lease_id != other.lease_id
    await lease.reserve()
    assert [a for a, _ in accessor.calls] == ["binding", "reserve"]
    assert lease._renew_task is not None
    await lease.hold()
    await lease.hold()
    await lease.aclose()
    assert [a for a, _ in accessor.calls] == ["binding", "reserve", "hold", "release"]
    with pytest.raises(RuntimeError):
        await lease.reserve()


@pytest.mark.asyncio
async def test_cancelled_reserve_compensated():
    accessor = Accessor()
    original = accessor.desktop_action

    async def action(name, args):
        result = await original(name, args)
        if name == "reserve":
            raise asyncio.CancelledError()
        return result

    accessor.desktop_action = action
    lease = DesktopLease(accessor, kind="mcp")
    with pytest.raises(asyncio.CancelledError):
        await lease.reserve()
    assert [a for a, _ in accessor.calls] == ["binding", "reserve", "release"]


@pytest.mark.asyncio
async def test_renew_rejection_interrupts_without_recreate(monkeypatch):
    accessor = Accessor()
    failed = []
    lease = DesktopLease(accessor, kind="mcp", on_failure=lambda: failed.append(True))
    original = accessor.desktop_action

    async def action(name, args):
        if name == "renew":
            return {"ok": False}
        return await original(name, args)

    accessor.desktop_action = action
    await lease.reserve()
    lease._renew_task.cancel()
    try:
        await lease._renew_task
    except asyncio.CancelledError:
        pass

    async def sleep(_):
        return None

    monkeypatch.setattr("apps.harness.desktop_leases.asyncio.sleep", sleep)
    await lease._renew()
    assert failed == [True]
    assert not lease.healthy
    with pytest.raises(RuntimeError):
        await lease.hold()
    await lease.aclose()


@pytest.mark.asyncio
async def test_first_tool_holds_after_lock_and_rechecks_closed():
    accessor = Accessor()
    lease = DesktopLease(accessor, kind="mcp")
    await lease.reserve()
    conn = McpServerConnection(
        plugin_id="p",
        plugin_slug="p",
        plugin_name="P",
        server_id="s",
        server_slug="s",
        server_name="S",
        transport="stdio",
        command=["cmd"],
        cwd="/workspace",
        desktop_lease=lease,
        desktop_activation="first_tool",
    )
    import anyio
    from mcp.types import CallToolResult, TextContent

    conn._lock = anyio.Lock()
    conn._session = AsyncMock()
    conn._session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="ok")]
    )
    await conn.call_tool("tool", {})
    assert [a for a, _ in accessor.calls] == ["binding", "reserve", "hold"]
    conn._closed = True
    with pytest.raises(Exception, match="not connected"):
        await conn.call_tool("tool", {})
    await lease.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("activation", ["server_start", "first_tool"])
async def test_managed_spawn_reserved_and_bound(activation, monkeypatch):
    from contextlib import asynccontextmanager

    accessor = Accessor()
    lease = DesktopLease(accessor, kind="mcp")
    conn = McpServerConnection(
        plugin_id="p",
        plugin_slug="p",
        plugin_name="P",
        server_id="s",
        server_slug="s",
        server_name="S",
        transport="stdio",
        command=["cmd"],
        cwd="/workspace",
        desktop_lease=lease,
        desktop_activation=activation,
    )

    @asynccontextmanager
    async def transport(accessor, command, **kwargs):
        assert kwargs["owner"] == lease.owner
        assert kwargs["env"]["DISPLAY"] == ":1"
        actions = [a for a, _ in accessor.calls]
        assert actions[:2] == ["binding", "reserve"]
        assert ("hold" in actions) == (activation == "server_start")
        yield None, None

    monkeypatch.setattr(
        "apps.harness.mcp_client.connection.workspace_stdio_client", transport
    )
    conn._open_session = AsyncMock()
    conn._discover = AsyncMock()
    await conn.open(accessor)
    await conn.aclose()
    assert accessor.calls[-1][0] == "release"


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fails", [False, True])
async def test_close_fences_gated_reserve_and_leaves_no_tasks(release_fails):
    accessor = Accessor()
    entered, resume = asyncio.Event(), asyncio.Event()
    original = accessor.desktop_action

    async def action(name, args):
        result = await original(name, args)
        if name == "reserve":
            entered.set()
            await resume.wait()
        if name == "release" and release_fails:
            raise RuntimeError("runner offline")
        return result

    accessor.desktop_action = action
    lease = DesktopLease(accessor, kind="mcp")
    reserve = asyncio.create_task(lease.reserve())
    await entered.wait()
    close = asyncio.create_task(lease.aclose())
    await asyncio.sleep(0)
    assert lease.closed
    resume.set()
    results = await asyncio.gather(reserve, close, return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert isinstance(results[1], RuntimeError) == release_fails
    assert lease._renew_task is None
    assert lease._close_task.done()
    assert "hold" not in [a for a, _ in accessor.calls]


@pytest.mark.asyncio
async def test_concurrent_close_joins_unknown_then_retries():
    accessor = Accessor()
    entered, resume = asyncio.Event(), asyncio.Event()
    original = accessor.desktop_action
    releases = 0

    async def action(name, args):
        nonlocal releases
        result = await original(name, args)
        if name == "release":
            releases += 1
            if releases == 1:
                entered.set()
                await resume.wait()
                result["lease_state"] = "closing"
        return result

    accessor.desktop_action = action
    lease = DesktopLease(accessor, kind="mcp")
    await lease.reserve()
    renewal = lease._renew_task
    first = asyncio.create_task(lease.aclose())
    await entered.wait()
    second = asyncio.create_task(lease.aclose())
    await asyncio.sleep(0)
    resume.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)
    assert releases == 1
    assert renewal.done()
    assert lease._renew_task is None
    await lease.aclose()
    await lease.aclose()
    assert releases == 2
    assert not lease.attempted


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reserve", "hold"])
async def test_cancelled_attempt_release_failure_preserves_cancellation(operation):
    accessor = Accessor()
    entered = asyncio.Event()
    original = accessor.desktop_action

    async def action(name, args):
        result = await original(name, args)
        if name == operation:
            entered.set()
            await asyncio.Event().wait()
        if name == "release":
            raise RuntimeError("offline")
        return result

    accessor.desktop_action = action
    lease = DesktopLease(accessor, kind="mcp")
    if operation == "hold":
        await lease.reserve()
    attempt = asyncio.create_task(getattr(lease, operation)())
    await entered.wait()
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    if operation == "hold":
        with pytest.raises(RuntimeError, match="offline"):
            await lease.aclose()
    assert lease._renew_task is None
    assert lease._close_task.done()
    assert accessor.calls[-1][0] == "release"


@pytest.mark.asyncio
async def test_hold_rejection_signals_owner_without_sdk_teardown():
    accessor = Accessor()
    original = accessor.desktop_action
    interrupted = []

    async def action(name, args):
        if name == "hold":
            return {"ok": False}
        return await original(name, args)

    accessor.desktop_action = action
    lease = DesktopLease(
        accessor, kind="mcp", on_failure=lambda: interrupted.append(True)
    )
    await lease.reserve()
    conn = McpServerConnection(
        plugin_id="p",
        plugin_slug="p",
        plugin_name="P",
        server_id="s",
        server_slug="s",
        server_name="S",
        transport="stdio",
        command=["cmd"],
        cwd="/workspace",
        desktop_lease=lease,
        desktop_activation="first_tool",
    )
    conn._session = AsyncMock()
    conn._stack = AsyncMock()
    with pytest.raises(Exception, match="failed"):
        await conn.call_tool("tool", {})
    assert interrupted == [True]
    assert not lease.healthy
    conn._stack.aclose.assert_not_awaited()
    conn._session.call_tool.assert_not_awaited()
    await conn.aclose()
