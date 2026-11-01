"""Agent claims against the real runner ledger and desktop lifecycle manager."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from apps.harness.access.environment import ExecutionEnvironmentAccessor
from apps.harness.agent_desktop import AgentDesktop
from apps.harness.mcp_client.connection import McpServerConnection
from apps.harness.mcp_client.runtime import McpRuntime
from apps.harness.tools.base import ToolContext, ToolRegistry
from apps.harness.tools.desktop import DesktopTool
from apps.plugins.bundled.computer_use import TOOL_POLICY
from apps.plugins.runtime_snapshot import (
    DesktopResourceSnapshot,
    EffectivePluginSnapshot,
    PluginMcpServerSnapshot,
    PluginRequirementSnapshot,
    PluginResourcesSnapshot,
    PreparedPluginRuntime,
    WorkspacePluginSnapshot,
)


@pytest.fixture
async def desktop_workspace(tmp_path, monkeypatch):
    # Exercise the actual RPC implementation without starting a VM or X server.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "runner"))
    from src.models import DesktopSession
    from src.services.sessions.desktop import DesktopManager
    from src.services.sessions.desktop_leases import DesktopLeaseStore

    workspace = uuid4()
    store = DesktopLeaseStore(tmp_path)
    running = False
    runtime = SimpleNamespace(
        get_workspace_status=AsyncMock(
            return_value=SimpleNamespace(instance_id="guest", status="running")
        )
    )
    manager = DesktopManager(
        get_cached=lambda _: SimpleNamespace(instance_id="guest"),
        get_runtime=lambda _: runtime,
        lease_store=store,
        epoch="epoch",
        close_owner_streams=AsyncMock(return_value=True),
    )

    async def start(*args, **kwargs):
        nonlocal running
        running = True
        manager._desktop_sessions.setdefault(
            workspace, DesktopSession(workspace, "guest")
        )

    async def stop(*args, **kwargs):
        nonlocal running
        running = False
        manager._desktop_sessions.pop(workspace, None)

    manager._ensure_desktop_process_locked = AsyncMock(side_effect=start)
    manager._stop_desktop_process = AsyncMock(side_effect=stop)
    manager._verify_binding = AsyncMock()
    manager._is_desktop_session_live = AsyncMock(side_effect=lambda _: running)

    class Accessor:
        workspace_id = str(workspace)

        async def desktop_action(self, action, args=None):
            return await manager.desktop_action(workspace, action, args)

    agents = []

    async def agent():
        desktop = AgentDesktop(Accessor(), str(uuid4()))
        agents.append(desktop)
        await desktop.prepare()
        return desktop

    yield SimpleNamespace(
        manager=manager,
        store=store,
        workspace=workspace,
        accessor=Accessor(),
        agent=agent,
    )
    for desktop in agents:
        await desktop.aclose()


async def test_status_distinguishes_other_claim_and_start_always_acquires_own(
    desktop_workspace,
):
    world = desktop_workspace
    viewer = dict(
        lease_id="viewer", owner_id="user", kind="viewer", epoch="epoch", revision=1
    )
    await world.accessor.desktop_action("hold", viewer)
    first, second = await world.agent(), await world.agent()
    status = await first.action("status")
    assert status == {
        "active": True,
        "own_claim": False,
        "own_claim_state": "reserved",
        "other_claim_count": 1,
        "held_by_others": True,
        "needs_start": True,
        "DISPLAY": ":1",
        "XAUTHORITY": "/root/.Xauthority",
    }
    await asyncio.gather(first.action("start"), second.action("start"))
    assert (await first.action("status"))["other_claim_count"] == 2
    # Closing the viewer cannot drop either agent's claim.
    await world.accessor.desktop_action("release", viewer)
    await first.action("stop")
    assert (await second.action("status"))["own_claim"]
    world.manager._stop_desktop_process.assert_not_awaited()
    status = await first.action("status")
    assert status["active"] and not status["own_claim"] and status["needs_start"]
    await second.action("stop")
    world.manager._stop_desktop_process.assert_awaited_once()


async def test_start_stop_idempotence_and_same_claim_across_cycles(desktop_workspace):
    world = desktop_workspace
    desktop = await world.agent()
    lease_id = desktop.lease.lease_id
    for _ in range(3):
        await asyncio.gather(*(desktop.action("start") for _ in range(5)))
        rows = await world.store.list_unfinished(str(world.workspace))
        assert len(rows) == 1 and rows[0]["activated"]
        await desktop.action("stop")
        status = await desktop.action("stop")
        assert not status["active"] and not status["own_claim"]
        assert desktop.lease.lease_id == lease_id
    assert world.manager._stop_desktop_process.await_count == 3
    # Stop releases activation, never the MCP transport owner.
    world.manager.close_owner_streams.assert_not_awaited()
    await desktop.aclose()
    world.manager.close_owner_streams.assert_awaited_once_with(lease_id)
    with pytest.raises(RuntimeError, match="closed"):
        await desktop.action("start")


def connection(desktop, name="computer-use", *, recording=False):
    conn = McpServerConnection(
        plugin_id="p",
        plugin_slug=name,
        plugin_name=name,
        server_id="s",
        server_slug="cua_repl",
        server_name="s",
        transport="stdio",
        command=[],
        cwd="/workspace",
        agent_desktop=desktop,
        desktop_lease=desktop.lease,
        tool_policy=TOOL_POLICY
        if name == "computer-use"
        else {"exclusive_input": True},
        run_context={"enable_recording": recording},
    )
    conn._accessor = desktop.accessor
    conn._session = AsyncMock()
    conn._session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="ok")]
    )
    desktop.before_stop.append(conn.stop_desktop_work)
    return conn


async def test_cua_playwright_and_explicit_action_share_one_claim(desktop_workspace):
    world = desktop_workspace
    desktop = await world.agent()
    cua, playwright = connection(desktop), connection(desktop, "playwright")
    await cua.call_tool("js", {"code": "await cua.getState()"})
    await playwright.call_tool("browser_navigate", {"url": "about:blank"})
    status = await desktop.action("start")
    assert status["own_claim"] and status["other_claim_count"] == 0
    assert len(await world.store.list_unfinished(str(world.workspace))) == 1
    assert cua._request_meta()["opencuria/context"]["display"] == ":1"
    # Stopping does not close connections, and a later operational call reclaims.
    await desktop.action("stop")
    assert not cua._closed and not playwright._closed
    cua._session.call_tool.assert_any_await(
        "turn_ended",
        {"session_id": "", "turn_id": "", "hook_event_name": "Stop"},
        meta=cua._request_meta(),
        read_timeout_seconds=timedelta(seconds=5),
    )
    await playwright.call_tool("browser_navigate", {"url": "about:blank"})
    assert (await desktop.action("status"))["own_claim"]
    await cua.call_tool("js", {"code": "await cua.getState()"})
    assert not cua._turn_ended


async def test_stop_waits_for_operation_and_blocks_same_agent_parallel_input(
    desktop_workspace,
):
    desktop = await desktop_workspace.agent()
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def operation():
        async with desktop.operation(exclusive_input=True):
            entered.set()
            await proceed.wait()

    running = asyncio.create_task(operation())
    await entered.wait()
    stopping = asyncio.create_task(desktop.action("stop"))
    await asyncio.sleep(0)
    assert not stopping.done()
    proceed.set()
    await running
    assert not (await stopping)["active"]
    async with desktop.operation(exclusive_input=True):
        other = await desktop_workspace.agent()
        with pytest.raises(RuntimeError, match="busy"):
            async with other.operation(exclusive_input=True):
                pytest.fail("Two agents got input ownership")


async def test_unconfirmed_stop_retains_protection_and_can_be_retried(
    desktop_workspace,
):
    world = desktop_workspace
    desktop = await world.agent()
    await desktop.action("start")
    original = world.manager._stop_desktop_process.side_effect
    world.manager._stop_desktop_process.side_effect = RuntimeError("unconfirmed stop")
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await desktop.action("stop")
    row = await world.store.get(desktop.lease.lease_id)
    assert row["activated"] and row["state"] == "held"
    world.manager._stop_desktop_process.side_effect = original
    assert not (await desktop.action("stop"))["own_claim"]


async def test_stale_hold_cannot_undo_stop_or_end_tombstone(desktop_workspace):
    world = desktop_workspace
    desktop = await world.agent()
    await desktop.action("start")
    old = {
        **desktop.lease._args(),
        "activation_revision": desktop.lease.activation_revision,
    }
    await desktop.action("stop")
    with pytest.raises(ValueError, match="Stale desktop activation"):
        await world.accessor.desktop_action("hold", old)
    assert not (await desktop.action("status"))["active"]
    await desktop.aclose()
    with pytest.raises(ValueError, match="ended"):
        await world.accessor.desktop_action("hold", {**old, "activation_revision": 999})


async def test_display_loss_revalidated_and_runner_restart_fails_closed(
    desktop_workspace,
):
    world = desktop_workspace
    desktop = await world.agent()
    await desktop.action("start")
    world.manager._is_desktop_session_live.side_effect = lambda _: False
    assert (await desktop.action("status"))["needs_start"]
    starts = world.manager._ensure_desktop_process_locked.await_count
    await desktop.action("start")
    assert world.manager._ensure_desktop_process_locked.await_count == starts + 1
    world.manager.epoch = "new-epoch"
    with pytest.raises(RuntimeError, match="stale"):
        await desktop.action("status")
    with pytest.raises(ValueError, match="Stale runner epoch"):
        await desktop.action("start")


async def test_tool_validation_and_run_bound_identity(desktop_workspace):
    world = desktop_workspace
    desktop = await world.agent()
    tool = DesktopTool(desktop)
    ctx = ToolContext(
        session_id="session", workspace_id=str(world.workspace), accessor=world.accessor
    )
    assert not json.loads((await tool.execute({"action": "status"}, ctx)).output)[
        "own_claim"
    ]
    for args in ({"action": "toggle"}, {"action": "start", "lease_id": "someone-else"}):
        with pytest.raises(ValidationError):
            await tool.execute(args, ctx)
    assert json.loads((await tool.execute({"action": "start"}, ctx)).output)[
        "own_claim"
    ]


async def test_execution_environment_is_run_local_and_helpers_stay_empty():
    raw = SimpleNamespace(
        exec_wait=AsyncMock(), open_process=AsyncMock(), process_start=AsyncMock()
    )
    binding = {"DISPLAY": ":1", "XAUTHORITY": "/root/.Xauthority"}
    accessor = ExecutionEnvironmentAccessor(raw, binding)
    await accessor.exec_wait("gui")
    assert raw.exec_wait.call_args.kwargs["env"] == binding
    await accessor.exec_wait("xvfb-test", env={"DISPLAY": ":99"})
    assert raw.exec_wait.call_args.kwargs["env"]["DISPLAY"] == ":99"
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        await accessor.open_process(
            ["agent"], env={key: "/config"}, owner={"lease_id": "owner", "epoch": "e"}
        )
        assert raw.open_process.call_args.args[2] == {**binding, key: "/config"}
    await accessor.open_process(["helper"], env={}, process_profile="helper")
    assert raw.open_process.call_args.args[2] == {}
    await accessor.process_start("gui", name="app", session_id="s", kind="temp")
    assert raw.process_start.call_args.kwargs["env"] == binding
    # A second run using the raw accessor does not inherit another run's policy.
    await raw.exec_wait("ordinary")
    assert "env" not in raw.exec_wait.call_args.kwargs


async def test_runtime_shares_binding_and_exposes_backend_tool_only_with_plugin(
    desktop_workspace, monkeypatch
):
    world = desktop_workspace
    opened = []

    async def open_connection(conn, accessor):
        opened.append(conn)
        conn._accessor = accessor
        conn._session = AsyncMock()
        return []

    monkeypatch.setattr(McpServerConnection, "open", open_connection)
    plugins = tuple(
        EffectivePluginSnapshot(
            id=uuid4(),
            name=slug,
            slug=slug,
            description="",
            organization_id=None,
            is_global=True,
            mcp_servers=(
                PluginMcpServerSnapshot(
                    id=uuid4(),
                    name=slug,
                    slug="cua_repl" if slug == "computer-use" else "browser",
                    transport="stdio",
                    command="server",
                    resources=PluginResourcesSnapshot(
                        desktop=DesktopResourceSnapshot(activation="first_tool")
                    ),
                ),
            ),
        )
        for slug in ("computer-use", "playwright")
    )
    prepared = PreparedPluginRuntime(
        WorkspacePluginSnapshot(
            workspace_id=world.workspace,
            organization_id=uuid4(),
            plugins=plugins,
        )
    )
    runtime = McpRuntime()
    try:
        await runtime.setup(prepared=prepared, accessor=world.accessor)
        assert len(opened) == 2
        assert opened[0].desktop_lease is opened[1].desktop_lease
        assert opened[0].agent_desktop is opened[1].agent_desktop
        assert not (await runtime.desktop.action("status"))["active"]
        registry = ToolRegistry()
        assert DesktopTool.name in runtime.register_tools(registry)
        assert isinstance(
            await runtime.execution_accessor(world.accessor),
            ExecutionEnvironmentAccessor,
        )
    finally:
        await runtime.aclose()
    assert not await world.store.list_unfinished(str(world.workspace))


async def test_recordings_are_finalized_and_unique_across_stop_start(desktop_workspace):
    world = desktop_workspace
    desktop = await world.agent()
    runtime = world.manager._get_runtime(world.workspace)
    runtime.probe_managed_token = AsyncMock(
        return_value={"boot_id": "boot", "init_starttime": "1"}
    )
    runtime.spawn_process = AsyncMock(return_value=object())
    runtime.process_wait = AsyncMock(return_value=0)
    runtime.close_managed_process = AsyncMock(return_value=True)
    runtime.process_close = AsyncMock(return_value=True)
    world.manager._get_desktop_geometry = AsyncMock(return_value=(1280, 720))
    cua = connection(desktop, recording=True)
    for _ in range(2):
        await cua.call_tool("js", {"code": "await cua.getState()"})
        await desktop.action("stop")
    paths = await desktop.finish()
    assert len(paths) == len(set(paths)) == 2
    rows = await world.store.recordings(desktop.lease.lease_id)
    assert {r["path"] for r in rows} == set(paths)
    assert all(r["state"] == "closed" for r in rows)
    assert not world.manager._recording_handles
    assert not world.manager._recording_waiters


async def test_cancelled_cua_fences_worker_before_releasing_shared_input(
    desktop_workspace,
):
    desktop = await desktop_workspace.agent()
    cua = connection(desktop)
    entered = asyncio.Event()
    order = []

    async def invoke(name, *args, **kwargs):
        if name == "js":
            entered.set()
            await asyncio.Event().wait()
        order.append("worker_stopped")
        return CallToolResult(content=[])

    cua._session.call_tool.side_effect = invoke
    original = desktop.accessor.desktop_action

    async def rpc(action, args=None):
        if action == "input_release":
            order.append("input_released")
        return await original(action, args)

    desktop.accessor.desktop_action = rpc
    task = asyncio.create_task(cua.call_tool("js", {"code": "busy()"}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order == ["worker_stopped", "input_released"]


async def test_uncertain_input_acquire_ends_owner_and_fences_late_work(
    desktop_workspace,
):
    world = desktop_workspace
    desktop = await world.agent()
    original = desktop.accessor.desktop_action

    async def rpc(action, args=None):
        result = await original(action, args)
        if action == "input_acquire":
            raise TimeoutError("lost acknowledgement")
        return result

    desktop.accessor.desktop_action = rpc
    with pytest.raises(TimeoutError):
        async with desktop.operation(exclusive_input=True):
            pytest.fail("Unknown input ownership executed a tool")
    assert (await world.store.get(desktop.lease.lease_id))["state"] == "released"
    with pytest.raises(RuntimeError, match="healthy"):
        await desktop.action("start")


async def test_expired_claim_is_not_reported_as_usable(desktop_workspace):
    import sqlite3

    world = desktop_workspace
    desktop = await world.agent()
    await desktop.action("start")
    with sqlite3.connect(world.store.path) as db:
        db.execute(
            "UPDATE desktop_leases SET expires_at=0 WHERE lease_id=?",
            (desktop.lease.lease_id,),
        )
    status = await desktop.action("status")
    assert status["active"] and not status["own_claim"] and status["needs_start"]
    assert not status["held_by_others"]
    await world.manager.maintain_workspace(world.workspace)
    assert not (await desktop.action("status"))["active"]


async def test_binding_without_plugin_does_not_create_any_claim(desktop_workspace):
    world = desktop_workspace
    runtime = McpRuntime()
    accessor = await runtime.execution_accessor(world.accessor)
    assert isinstance(accessor, ExecutionEnvironmentAccessor)
    assert not await world.store.list_unfinished(str(world.workspace))
    world.manager._ensure_desktop_process_locked.assert_not_awaited()
    assert runtime.register_tools(ToolRegistry()) == []


def test_desktop_tool_uses_shared_role_and_permission_gates(fake_accessor):
    from apps.harness.agents.definitions import get_agent
    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.tools.invocation import AuthorizedToolDispatcher

    desktop = AgentDesktop(fake_accessor, str(uuid4()))
    registry = ToolRegistry()
    registry.register(DesktopTool(desktop))
    dispatcher = AuthorizedToolDispatcher(
        tools=registry,
        accessor=fake_accessor,
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
    )
    assert (
        dispatcher._decide(get_agent("build"), DesktopTool.name, "desktop", "build")
        == "allow"
    )
    assert (
        dispatcher._decide(get_agent("plan"), DesktopTool.name, "desktop", "plan")
        == "ask"
    )
    assert (
        dispatcher._decide(get_agent("explore"), DesktopTool.name, "desktop", "build")
        == "deny"
    )
    denied = AuthorizedToolDispatcher(
        tools=registry,
        accessor=fake_accessor,
        evaluator=PermissionEvaluator(global_rules={DesktopTool.name: "deny"}),
    )
    assert (
        denied._decide(get_agent("build"), DesktopTool.name, "desktop", "build")
        == "deny"
    )


@pytest.fixture
def scoped_desktop_prepared(desktop_workspace):
    world = desktop_workspace
    plugins = tuple(
        EffectivePluginSnapshot(
            id=uuid4(),
            name=slug,
            slug=slug,
            description="",
            organization_id=None,
            is_global=True,
            mcp_servers=(
                PluginMcpServerSnapshot(
                    id=uuid4(),
                    name=slug,
                    slug="cua_repl" if slug == "computer-use" else "browser",
                    transport="stdio",
                    command="server",
                    tool_policy={"exclusive_input": True},
                    resources=PluginResourcesSnapshot(
                        desktop=DesktopResourceSnapshot(activation="first_tool")
                    ),
                ),
            ),
        )
        for slug in ("computer-use", "playwright")
    )
    return PreparedPluginRuntime(
        WorkspacePluginSnapshot(
            workspace_id=world.workspace,
            organization_id=uuid4(),
            plugins=plugins,
        )
    )


@pytest.fixture
async def scoped_desktop_runtime(
    desktop_workspace, scoped_desktop_prepared, monkeypatch
):
    from apps.harness.mcp_client.connection import DiscoveredMcpTool
    from apps.harness.mcp_client.naming import mcp_tool_name

    world = desktop_workspace

    async def open_connection(conn, accessor):
        conn._accessor = accessor
        conn._session = AsyncMock()
        conn._session.call_tool.return_value = CallToolResult(
            content=[TextContent(type="text", text="ok")]
        )
        conn.tools = [
            DiscoveredMcpTool(
                "operate",
                mcp_tool_name(conn.plugin_slug, conn.server_slug, "operate"),
                "Desktop operation",
                {"type": "object"},
                "Operate",
            )
        ]
        return conn.tools

    monkeypatch.setattr(McpServerConnection, "open", open_connection)
    runtime = McpRuntime()
    await runtime.setup(prepared=scoped_desktop_prepared, accessor=world.accessor)
    registry = ToolRegistry()
    runtime.register_tools(registry)
    yield runtime, registry
    await runtime.aclose()


async def test_native_child_agents_have_independent_claims_and_lifetimes(
    desktop_workspace, scoped_desktop_runtime
):
    from apps.harness.mcp_client.scopes import close_desktop_agent_scope

    runtime, registry = scoped_desktop_runtime
    tool = registry.get(DesktopTool.name)
    contexts = [
        ToolContext(
            session_id="root",
            workspace_id=str(desktop_workspace.workspace),
            accessor=desktop_workspace.accessor,
            agent_scope=scope,
        )
        for scope in ("codex:child:turn-1", "claude:child-call")
    ]
    await asyncio.gather(*(tool.execute({"action": "start"}, ctx) for ctx in contexts))
    root = await runtime.desktop.action("status")
    assert root["active"] and not root["own_claim"] and root["other_claim_count"] == 2
    for ctx in contexts:
        child_tools = await runtime.agent_scopes.tools(ctx.agent_scope)
        # Headed Playwright and the action use this child's one claim.
        await child_tools.get("mcp_playwright_browser_operate").execute({}, ctx)
    root = await runtime.desktop.action("status")
    assert root["other_claim_count"] == 2
    await close_desktop_agent_scope(registry, contexts[0].agent_scope)
    status = json.loads((await tool.execute({"action": "status"}, contexts[1])).output)
    assert status["own_claim"] and status["other_claim_count"] == 0
    with pytest.raises(Exception, match="ended"):
        await tool.execute({"action": "start"}, contexts[0])
    await close_desktop_agent_scope(registry, contexts[1].agent_scope)
    assert not (await runtime.desktop.action("status"))["active"]
    assert (await runtime.desktop.action("start"))["own_claim"]


async def test_native_child_close_cancels_only_its_pending_tool(
    desktop_workspace, scoped_desktop_runtime
):
    runtime, registry = scoped_desktop_runtime
    ctx = ToolContext(
        session_id="root",
        workspace_id=str(desktop_workspace.workspace),
        accessor=desktop_workspace.accessor,
        agent_scope="codex:child:turn-1",
    )
    child_tools = await runtime.agent_scopes.tools(ctx.agent_scope)
    conn = child_tools.get("mcp_playwright_browser_operate")._connection
    entered = asyncio.Event()

    async def busy(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    conn._session.call_tool.side_effect = busy
    task = asyncio.create_task(
        registry.get("mcp_playwright_browser_operate").execute({}, ctx)
    )
    await entered.wait()
    await runtime.agent_scopes.close(ctx.agent_scope)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await runtime.desktop.action("start"))["own_claim"]
    assert all(not conn._closed for conn in runtime.connections)


async def test_native_scope_actors_own_real_sdk_scopes_and_close_lifo(
    desktop_workspace,
):
    from apps.harness.tests.test_mcp_startup_lifecycle import ScriptedProcess

    world = desktop_workspace
    opened, closed = [], []

    class Process(ScriptedProcess):
        async def send(self, data):
            request = json.loads(data)
            if request["method"] == "tools/call":
                await self.replies.put(
                    (
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "result": {"content": [{"type": "text", "text": "ok"}]},
                            }
                        )
                        + "\n"
                    ).encode()
                )
            else:
                await super().send(data)

    async def open_process(
        command, workdir="/workspace", env=None, timeout=None, *, owner=None
    ):
        process = Process(owner["lease_id"], None, closed)
        opened.append(process)
        return process

    world.accessor.open_process = open_process
    plugin = EffectivePluginSnapshot(
        id=uuid4(),
        name="Computer Use",
        slug="computer-use",
        description="",
        organization_id=None,
        is_global=True,
        mcp_servers=(
            PluginMcpServerSnapshot(
                id=uuid4(),
                name="CUA",
                slug="cua_repl",
                transport="stdio",
                command="fixture",
                resources=PluginResourcesSnapshot(
                    desktop=DesktopResourceSnapshot(activation="first_tool")
                ),
            ),
        ),
    )
    runtime = McpRuntime()
    registry = ToolRegistry()
    try:
        await runtime.setup(
            prepared=PreparedPluginRuntime(
                WorkspacePluginSnapshot(
                    workspace_id=world.workspace,
                    organization_id=uuid4(),
                    plugins=(plugin,),
                )
            ),
            accessor=world.accessor,
        )
        runtime.register_tools(registry)
        tool = registry.get(DesktopTool.name)
        for scope in ("codex:child:a", "claude:child-b"):
            ctx = ToolContext(
                session_id="root",
                workspace_id=str(world.workspace),
                accessor=world.accessor,
                agent_scope=scope,
            )
            await tool.execute({"action": "start"}, ctx)
            await registry.get("mcp_computer-use_cua_repl_echo").execute({}, ctx)
            await runtime.agent_scopes.close(scope)
        # The parent's entered SDK scope remains usable after both child exits.
        assert runtime.connections[0]._session is not None
        await runtime.connections[0]._session.list_tools()
        assert len(opened) == 3 and len(closed) == 2
        assert len({process.name for process in opened}) == 3
    finally:
        await runtime.aclose()
    assert len(closed) == 3
    assert all(process.close_count == 1 for process in opened)
    assert not await world.store.list_unfinished(str(world.workspace))


async def test_lost_recording_ack_can_stop_without_inventing_video(desktop_workspace):
    world = desktop_workspace
    desktop = await world.agent()
    runtime = world.manager._get_runtime(world.workspace)
    runtime.probe_managed_token = AsyncMock(
        return_value={"boot_id": "boot", "init_starttime": "1"}
    )
    runtime.spawn_process = AsyncMock(return_value=object())
    runtime.process_wait = AsyncMock(return_value=0)
    runtime.close_managed_process = AsyncMock(return_value=True)
    runtime.process_close = AsyncMock(return_value=True)
    world.manager._get_desktop_geometry = AsyncMock(return_value=(1280, 720))
    original = desktop.accessor.desktop_action

    async def rpc(action, args=None):
        result = await original(action, args)
        if action == "record_start":
            raise TimeoutError("lost recording acknowledgement")
        return result

    desktop.accessor.desktop_action = rpc
    with pytest.raises(TimeoutError):
        async with desktop.operation(exclusive_input=True):
            await desktop.record()
    assert not (await desktop.action("stop"))["own_claim"]
    assert await desktop.finish() == []
    assert all(
        row["state"] == "closed"
        for row in await world.store.recordings(desktop.lease.lease_id)
    )


async def test_failed_worker_stop_ends_claim_before_allowing_more_input(
    desktop_workspace,
):
    world = desktop_workspace
    desktop = await world.agent()
    cua = connection(desktop)
    await cua.call_tool("js", {"code": "await cua.getState()"})
    cua._session.call_tool.side_effect = RuntimeError("worker transport lost")
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        await desktop.action("stop")
    assert (await world.store.get(desktop.lease.lease_id))["state"] == "released"
    with pytest.raises(RuntimeError, match="healthy"):
        await desktop.action("start")


@pytest.mark.parametrize(
    "exposed,visible",
    [(None, True), (["desktop"], True), ([], False), (["operate"], False)],
)
async def test_backend_desktop_tool_respects_plugin_exposure(
    scoped_desktop_runtime, exposed, visible
):
    runtime, _ = scoped_desktop_runtime
    cua = next(
        conn for conn in runtime.connections if conn.plugin_slug == "computer-use"
    )
    cua.tool_policy["exposed_tools"] = exposed
    registry = ToolRegistry()
    runtime.register_tools(registry)
    assert (DesktopTool.name in registry.names()) is visible


async def test_playwright_only_preserves_generic_mcp_status(
    desktop_workspace, scoped_desktop_prepared, scoped_desktop_runtime
):
    prepared = scoped_desktop_prepared
    prepared = replace(
        prepared,
        snapshot=replace(
            prepared.snapshot,
            plugins=tuple(
                plugin
                for plugin in prepared.snapshot.plugins
                if plugin.slug == "playwright"
            ),
        ),
    )
    runtime = McpRuntime()
    world = desktop_workspace
    try:
        await runtime.setup(prepared=prepared, accessor=world.accessor)
        assert runtime.desktop.kind == "mcp"
        await runtime.desktop.action("start")
        status = await world.manager._lease_projection(world.workspace)
        assert status["mcp_active"] and not status["computer_use_active"]
        assert not (await runtime.desktop.action("stop"))["active"]
    finally:
        await runtime.aclose()


async def test_parent_cancellation_still_joins_child_desktop_cleanup(
    desktop_workspace, scoped_desktop_runtime
):
    import anyio

    runtime, registry = scoped_desktop_runtime
    ctx = ToolContext(
        session_id="root",
        workspace_id=str(desktop_workspace.workspace),
        accessor=desktop_workspace.accessor,
        agent_scope="codex:child:turn-1",
    )
    await registry.get(DesktopTool.name).execute({"action": "start"}, ctx)
    with anyio.CancelScope() as cancelled:
        cancelled.cancel()
        await runtime.agent_scopes.aclose()
    status = await runtime.desktop.action("status")
    assert not status["active"] and status["other_claim_count"] == 0
    assert (await runtime.desktop.action("start"))["own_claim"]


async def test_child_scope_opens_only_desktop_servers_with_frozen_config(
    desktop_workspace, scoped_desktop_prepared, scoped_desktop_runtime, monkeypatch
):
    original_open = McpServerConnection.open
    opened = []

    async def track_open(connection, accessor):
        opened.append(connection)
        return await original_open(connection, accessor)

    monkeypatch.setattr(McpServerConnection, "open", track_open)
    desktop_plugin = scoped_desktop_prepared.snapshot.plugins[1]
    desktop_server = replace(
        desktop_plugin.mcp_servers[0], env={"TOKEN": "{{credential.token}}"}
    )
    ordinary_server = replace(
        desktop_server, id=uuid4(), slug="ordinary", resources=PluginResourcesSnapshot()
    )
    mixed_plugin = replace(
        desktop_plugin,
        mcp_servers=(desktop_server, ordinary_server),
        requirements=(
            PluginRequirementSnapshot(
                id=uuid4(),
                key="token",
                required=True,
                description="",
                service_id=uuid4(),
                service_slug="token",
                service_name="Token",
            ),
        ),
    )
    ordinary_plugin = replace(
        mixed_plugin, id=uuid4(), slug="ordinary", mcp_servers=(ordinary_server,)
    )
    prepared = replace(
        scoped_desktop_prepared,
        snapshot=replace(
            scoped_desktop_prepared.snapshot, plugins=(mixed_plugin, ordinary_plugin)
        ),
        plaintexts={
            mixed_plugin.id: {"token": "frozen-token"},
            ordinary_plugin.id: {"token": "frozen-token"},
        },
    )
    runtime = McpRuntime()
    try:
        await runtime.setup(
            prepared=prepared,
            accessor=desktop_workspace.accessor,
            session_id="session",
            turn_id="turn",
            runtime_options={"option": "value"},
        )
        assert len(opened) == 3
        opened.clear()
        first = await runtime.agent_scopes.tools("child-one")
        assert await runtime.agent_scopes.tools("child-one") is first
        second = await runtime.agent_scopes.tools("child-two")
        assert first is not second
        assert first.names() == second.names() == ["mcp_playwright_browser_operate"]
        assert len(opened) == 2
        assert len({conn.agent_desktop.lease.owner_id for conn in opened}) == 2
        for conn in opened:
            assert conn.env == {"TOKEN": "frozen-token"}
            assert conn.run_context == {
                "session_id": "session",
                "turn_id": "turn",
                "option": "value",
            }
        assert prepared.snapshot.plugins == (mixed_plugin, ordinary_plugin)
    finally:
        await runtime.aclose()
    assert all(conn._closed for conn in opened)


async def test_child_recordings_are_collected_before_connections_close(
    scoped_desktop_runtime, monkeypatch
):
    runtime, _ = scoped_desktop_runtime
    await runtime.agent_scopes.tools("child")
    finished = []

    async def finish(connection):
        assert not connection._closed
        if not connection.recording_path:
            owner = connection.agent_desktop.lease.owner_id
            connection.recording_path = f"/workspace/{owner}-{connection.server_id}.mp4"
            finished.append(connection.recording_path)

    monkeypatch.setattr(McpServerConnection, "finish", finish)
    await runtime.agent_scopes.close("child")
    child_paths = list(finished)
    assert len(child_paths) == 2
    assert runtime.agent_scopes.recordings == child_paths
    paths = await runtime.finish()
    assert len(paths) == 4 and set(paths) == set(finished)
    assert set(child_paths) <= set(paths)
    assert await runtime.finish() == paths


async def test_failed_child_registration_closes_resources_and_preserves_parent(
    scoped_desktop_runtime, monkeypatch
):
    runtime, registry = scoped_desktop_runtime
    original_register = McpRuntime.register_tools
    child_connections = []

    def fail_registration(child, tools):
        child_connections.extend(child.connections)
        original_register(child, tools)
        raise RuntimeError("registration failed")

    monkeypatch.setattr(McpRuntime, "register_tools", fail_registration)
    with pytest.raises(RuntimeError, match="registration failed"):
        await runtime.agent_scopes.tools("failed-child")
    with pytest.raises(RuntimeError, match="registration failed"):
        await runtime.agent_scopes.close("failed-child")
    assert len(child_connections) == 2
    assert all(connection._closed for connection in child_connections)
    assert all(not connection._closed for connection in runtime.connections)
    assert registry.get(DesktopTool.name)
    assert (await runtime.desktop.action("start"))["own_claim"]
