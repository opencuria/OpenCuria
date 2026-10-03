"""Startup/teardown regressions using real AnyIO scopes and MCP sessions.

Only the workspace byte-stream boundary is fake: open(), stdio pumps,
ClientSession, initialization, discovery and runtime ownership are production code.
All successful opens and their closes run in the same task, in strict LIFO order.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import anyio
import pytest
from mcp.client.session import ClientSession

from apps.harness.mcp_client.connection import McpServerConnection, McpServerHealthError
from apps.harness.mcp_client.runtime import McpRuntime
from apps.plugins.runtime_snapshot import (
    EffectivePluginSnapshot,
    PluginMcpServerSnapshot,
    PreparedPluginRuntime,
    WorkspacePluginSnapshot,
)


class ScriptedProcess:
    """JSON-RPC peer that can stop replying at an exact startup phase."""

    def __init__(self, name: str, stall: str | None, closed: list[str]) -> None:
        self.name = name
        self.stall = stall
        self.closed = closed
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()
        self.reached = asyncio.Event()
        self.methods: list[str] = []
        self.close_count = 0
        self.eof_count = 0

    async def receive(self) -> bytes:
        return await self.replies.get()

    async def send(self, data: bytes) -> None:
        request = json.loads(data)
        method = request["method"]
        self.methods.append(method)
        phase = {"initialize": "initialize", "tools/list": "discovery"}.get(method)
        if phase == self.stall:
            self.reached.set()
            return
        if "id" not in request:
            return
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": "1"},
            }
        else:
            assert method == "tools/list"
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        await self.replies.put(
            json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            ).encode()
            + b"\n"
        )

    async def send_eof(self) -> None:
        await anyio.sleep(0)
        self.eof_count += 1

    async def aclose(self) -> None:
        await anyio.sleep(0)
        self.close_count += 1
        self.closed.append(self.name)


class ScriptedAccessor:
    def __init__(self, stalls: dict[str, str | None]) -> None:
        self.closed: list[str] = []
        self.processes = {
            name: ScriptedProcess(name, stall, self.closed)
            for name, stall in stalls.items()
        }

    async def open_process(
        self, command, workdir="/workspace", env=None, timeout=None
    ) -> ScriptedProcess:
        process = self.processes[command[0]]
        if process.stall == "transport":
            process.reached.set()
            await anyio.sleep_forever()
        return process


def connection(name: str, timeout: float) -> McpServerConnection:
    return McpServerConnection(
        plugin_id=uuid.uuid4(),
        plugin_slug="plug",
        plugin_name="Plug",
        server_id=uuid.uuid4(),
        server_slug=name,
        server_name=name,
        transport="stdio",
        command=[name],
        cwd="/workspace",
        startup_timeout_seconds=timeout,
        request_timeout_seconds=10,
    )


def prepared_servers(*names: str) -> PreparedPluginRuntime:
    plugin = EffectivePluginSnapshot(
        id=uuid.uuid4(),
        name="Plug",
        slug="plug",
        description="",
        organization_id=None,
        is_global=True,
        mcp_servers=tuple(
            PluginMcpServerSnapshot(
                id=uuid.uuid4(),
                name=name,
                slug=name,
                transport="stdio",
                command=name,
                startup_timeout_seconds=0.05 if name == "bad" else 5,
                request_timeout_seconds=10,
            )
            for name in names
        ),
    )
    return PreparedPluginRuntime(
        snapshot=WorkspacePluginSnapshot(
            workspace_id=uuid.uuid4(), organization_id=uuid.uuid4(), plugins=(plugin,)
        ),
        workspace=None,
        plaintexts={plugin.id: {}},
    )


async def setup(runtime: McpRuntime, accessor: ScriptedAccessor) -> None:
    prepared = prepared_servers(*accessor.processes)
    await runtime.setup(
        workspace=None,
        organization_id=prepared.snapshot.organization_id,
        accessor=accessor,
        snapshot=prepared,
    )


async def assert_no_residual_cancellation(*, live_pumps: int = 0) -> None:
    # AnyIO cancellation is level-triggered: a leaked cancelled scope would
    # cancel this checkpoint even if the original exception was caught.
    await anyio.sleep(0)
    task = asyncio.current_task()
    assert task is not None
    assert task.cancelling() == 0
    assert (
        sum(
            task.get_name().startswith("mcp-stdio-pumps-")
            for task in asyncio.all_tasks()
            if not task.done()
        )
        == live_pumps
    )


@pytest.mark.parametrize("phase", ["transport", "initialize", "discovery"])
async def test_startup_deadline_is_health_error(phase: str) -> None:
    accessor = ScriptedAccessor({"bad": phase})
    conn = connection("bad", 0.05)
    with pytest.raises(McpServerHealthError, match="startup timed out") as error:
        await conn.open(accessor)
    assert isinstance(error.value.__cause__, TimeoutError)
    assert accessor.processes["bad"].reached.is_set()
    assert conn._stack is None
    assert accessor.closed == ([] if phase == "transport" else ["bad"])
    process = accessor.processes["bad"]
    assert process.close_count == (0 if phase == "transport" else 1)
    assert (process.eof_count > 0) == (phase != "transport")
    await conn.aclose()
    await conn.aclose()
    await assert_no_residual_cancellation()


@pytest.mark.parametrize("phase", ["transport", "initialize", "discovery"])
async def test_explicit_cancel_is_not_health_error(phase: str) -> None:
    accessor = ScriptedAccessor({"bad": phase})
    conn = connection("bad", 5)
    task = asyncio.create_task(conn.open(accessor))
    await asyncio.wait_for(accessor.processes["bad"].reached.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert conn._stack is None
    assert accessor.closed == ([] if phase == "transport" else ["bad"])
    process = accessor.processes["bad"]
    assert process.close_count == (0 if phase == "transport" else 1)
    assert (process.eof_count > 0) == (phase != "transport")
    await assert_no_residual_cancellation()


@pytest.mark.parametrize("phase", ["transport", "initialize", "discovery"])
async def test_runtime_skips_later_timeout_and_closes_healthy_lifo(phase: str) -> None:
    accessor = ScriptedAccessor({"first": None, "second": None, "bad": phase})
    runtime = McpRuntime()
    try:
        await setup(runtime, accessor)
        assert [conn.server_slug for conn in runtime.connections] == ["first", "second"]
        assert all(
            isinstance(conn._session, ClientSession) for conn in runtime.connections
        )
        assert len(runtime.tools) == 2
        assert len(runtime.skipped) == 1
        assert runtime.skipped[0]["server"] == "bad"
        assert "McpServerHealthError" in runtime.skipped[0]["error"]
        assert "startup timed out" in runtime.skipped[0]["error"]
        await assert_no_residual_cancellation(live_pumps=2)
        # Earlier sessions must remain usable after the later deadline fires.
        for conn in runtime.connections:
            page = await conn._session.list_tools()
            assert [tool.name for tool in page.tools] == ["echo"]
    finally:
        await runtime.aclose()
    await runtime.aclose()
    prefix = [] if phase == "transport" else ["bad"]
    assert accessor.closed == prefix + ["second", "first"]
    assert all(
        accessor.processes[name].close_count == 1 for name in ("first", "second")
    )
    assert runtime.connections == []
    await assert_no_residual_cancellation()


@pytest.mark.parametrize("phase", ["transport", "initialize", "discovery"])
async def test_cancelled_setup_closes_already_opened_connections(phase: str) -> None:
    accessor = ScriptedAccessor({"first": None, "second": None, "bad": phase})
    runtime = McpRuntime()

    async def owner() -> None:
        try:
            await setup(runtime, accessor)
        finally:
            await runtime.aclose()

    task = asyncio.create_task(owner())
    await asyncio.wait_for(accessor.processes["bad"].reached.wait(), timeout=2)
    assert [conn.server_slug for conn in runtime.connections] == ["first", "second"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    prefix = [] if phase == "transport" else ["bad"]
    assert accessor.closed == prefix + ["second", "first"]
    assert all(
        accessor.processes[name].close_count == 1 for name in ("first", "second")
    )
    assert runtime.connections == []
    assert runtime.skipped == []
    assert task.cancelled()


@pytest.mark.parametrize("cancel_server", ["first", "second"])
async def test_late_sdk_session_cancellation_closes_healthy_lifo(
    cancel_server: str,
) -> None:
    accessor = ScriptedAccessor({"first": None, "second": None})
    runtime = McpRuntime()
    try:
        await setup(runtime, accessor)
        conn = next(c for c in runtime.connections if c.server_slug == cancel_server)
        assert conn._session is not None
        # A real SDK session can cancel its scope after successful startup.
        # In particular, cancelling the first scope also cancels its nested
        # second connection. Closing the latter must still finish stream I/O.
        conn._session._task_group.cancel_scope.cancel()
        with pytest.raises(asyncio.CancelledError):
            await anyio.sleep(0)
    finally:
        await runtime.aclose()
    await runtime.aclose()
    assert accessor.closed == ["second", "first"]
    assert all(
        p.close_count == 1 and p.eof_count >= 1 for p in accessor.processes.values()
    )
    await assert_no_residual_cancellation()


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "task-cancel"])
@pytest.mark.parametrize("transport", ["streamable_http", "sse"])
async def test_http_startup_stall_closes_checkpointing_stream(
    cancel: bool,
    transport: str,
) -> None:
    """HTTP SDK/httpcore teardown already shields workspace TCP cleanup."""
    from apps.harness.tests.test_mcp_workspace_http import FakeByteStream

    class CheckpointStream(FakeByteStream):
        async def send_eof(self) -> None:
            await anyio.sleep(0)
            await super().send_eof()

        async def aclose(self) -> None:
            await anyio.sleep(0)
            await super().aclose()

    class Accessor:
        def __init__(self) -> None:
            self.streams: list[CheckpointStream] = []
            self.opened = asyncio.Event()

        async def open_tcp(
            self, host, port, tls=False, server_hostname=None, timeout=None
        ) -> CheckpointStream:
            stream = CheckpointStream([])
            self.streams.append(stream)
            self.opened.set()
            return stream

    accessor = Accessor()
    conn = connection("http", 5 if cancel else 0.05)
    conn.transport = transport
    conn.url = "http://localhost:8123/mcp"
    if cancel:
        task = asyncio.create_task(conn.open(accessor))
        await asyncio.wait_for(accessor.opened.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    else:
        with pytest.raises(McpServerHealthError, match="startup timed out"):
            await conn.open(accessor)
    assert accessor.streams
    assert all(s.eof_sent and s.closed for s in accessor.streams)
    assert conn._stack is None
    await assert_no_residual_cancellation()
