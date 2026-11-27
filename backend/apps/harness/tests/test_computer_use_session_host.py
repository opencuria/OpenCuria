"""A chat keeps one Computer Use REPL process and desktop lease across its runs."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from apps.harness.computer_use.session_host import session_hosts
from apps.harness.mcp_client.runtime import McpRuntime
from apps.harness.runner_gateway import RunnerHarnessGateway
from apps.harness.tests.test_agent_desktop import desktop_workspace  # noqa: F401
from apps.harness.tools.base import ToolContext, ToolRegistry
from apps.harness.tools.desktop import DesktopTool
from apps.plugins.bundled.computer_use import PLUGIN_ID, TOOL_POLICY
from apps.plugins.runtime_snapshot import (
    DesktopResourceSnapshot,
    EffectivePluginSnapshot,
    PluginMcpServerSnapshot,
    PluginResourcesSnapshot,
    PreparedPluginRuntime,
    WorkspacePluginSnapshot,
)

JS = "mcp_computer-use_cua_repl_js"
SERVER_ID = uuid4()


class ReplProcess:
    """Stdio Computer Use server: keeps variables, fences stopped turns."""

    def __init__(self) -> None:
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()
        self.calls: list[tuple[str, dict, dict]] = []
        self.variables: dict[str, str] = {}
        self.fenced: set[str] = set()
        self.release = asyncio.Event()
        self.release.set()
        self.close_count = 0
        self.closed_in: asyncio.Task | None = None

    async def receive(self) -> bytes:
        return await self.replies.get()

    async def send(self, data: bytes) -> None:
        request = json.loads(data)
        if "id" not in request:
            return
        method = request["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cua", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "inputSchema": {"type": "object"}}
                    for name in ("js", "js_reset", "turn_ended")
                ]
            }
        elif method == "tools/call":
            asyncio.ensure_future(self._call(request))
            return
        else:
            assert method == "ping"
            result = {}
        await self._reply(request["id"], result)

    async def _call(self, request: dict) -> None:
        params = request["params"]
        name, args = params["name"], params.get("arguments") or {}
        meta = (params.get("_meta") or {}).get("opencuria/context", {})
        self.calls.append((name, args, meta))
        if name == "turn_ended":
            if args["hook_event_name"] == "Stop":
                self.fenced.add(args["turn_id"])
            text = "ok"
        elif meta.get("turn_id") in self.fenced:
            await self._reply(
                request["id"],
                {"content": [{"type": "text", "text": "fenced"}], "isError": True},
            )
            return
        else:
            await self.release.wait()
            code = args["code"]
            if "=" in code:
                key, value = (part.strip() for part in code.split("=", 1))
                self.variables[key] = value
                text = value
            else:
                text = self.variables.get(code, "undefined")
        await self._reply(request["id"], {"content": [{"type": "text", "text": text}]})

    async def _reply(self, request_id: int, result: dict) -> None:
        message = {"jsonrpc": "2.0", "id": request_id, "result": result}
        await self.replies.put(json.dumps(message).encode() + b"\n")

    def turn_events(self) -> list[tuple[str, str]]:
        return [
            (args["turn_id"], args["hook_event_name"])
            for name, args, _ in self.calls
            if name == "turn_ended"
        ]

    async def send_eof(self) -> None:
        await asyncio.sleep(0)

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self.close_count += 1
        self.closed_in = asyncio.current_task()


def prepared(world, *, version: str = "v1", plugins: bool = True):
    server = PluginMcpServerSnapshot(
        id=SERVER_ID,
        name="CUA",
        slug="cua_repl",
        transport="stdio",
        command="cua",
        tool_policy=dict(TOOL_POLICY),
        resources=PluginResourcesSnapshot(
            desktop=DesktopResourceSnapshot(activation="first_tool")
        ),
        updated_at=version,
    )
    plugin = EffectivePluginSnapshot(
        id=PLUGIN_ID,
        name="Computer Use",
        slug="computer-use",
        description="",
        organization_id=None,
        is_global=True,
        mcp_servers=(server,),
    )
    return PreparedPluginRuntime(
        WorkspacePluginSnapshot(
            workspace_id=world.workspace,
            organization_id=uuid4(),
            plugins=(plugin,) if plugins else (),
        )
    )


@pytest.fixture
async def world(desktop_workspace):  # noqa: F811
    processes: list[ReplProcess] = []

    async def open_process(
        command, workdir="/workspace", env=None, timeout=None, *, owner=None
    ):
        process = ReplProcess()
        processes.append(process)
        return process

    desktop_workspace.accessor.open_process = open_process
    desktop_workspace.processes = processes
    yield desktop_workspace
    await session_hosts.close_all(reason="test")


async def rows(world):
    return await world.store.list_unfinished(str(world.workspace))


def run(world, *, chat="chat", turn="turn-1", plan=None, options=None, version="v1"):
    """One run like the engines: setup, tools, finish, then aclose in finally."""

    async def body():
        runtime = McpRuntime()
        registry = ToolRegistry()
        ctx = ToolContext(
            session_id=chat, workspace_id=str(world.workspace), accessor=world.accessor
        )

        async def js(code):
            return (await registry.get(JS).execute({"code": code}, ctx)).output

        async def desktop(action):
            tool = registry.get(DesktopTool.name)
            return json.loads((await tool.execute({"action": action}, ctx)).output)

        try:
            await runtime.setup(
                prepared=prepared(world, version=version),
                accessor=world.accessor,
                owner_id=turn,
                session_id=chat,
                turn_id=turn,
                runtime_options=dict(options or {}),
            )
            runtime.register_tools(registry)
            result = SimpleNamespace(runtime=runtime, js=js, desktop=desktop)
            result.connection = next(
                c for c in runtime.connections if c.plugin_slug == "computer-use"
            )
            result.value = await plan(result) if plan else None
            result.recordings = await runtime.finish()
            return result
        finally:
            await runtime.aclose()

    return asyncio.create_task(body())


async def test_runs_of_one_chat_share_state_process_and_reserved_lease(world):
    first = await run(world, plan=lambda r: r.js("x = 41"))
    host = session_hosts.get("chat")
    assert host is not None and not host.attached and not host.closing
    lease_id = host.desktop.lease.lease_id
    # Run end: turn stopped, display claim released, reservation kept.
    assert [
        (r["lease_id"], r["state"], bool(r["activated"])) for r in await rows(world)
    ] == [(lease_id, "reserved", False)]
    world.manager._stop_desktop_process.assert_awaited_once()

    second = await run(world, turn="turn-2", plan=lambda r: r.js("x"))
    assert second.value == "41"
    assert first.connection is second.connection is host.connection
    assert session_hosts.get("chat") is host
    [process] = world.processes
    js_turns = [meta["turn_id"] for name, _, meta in process.calls if name == "js"]
    assert js_turns == ["turn-1", "turn-2"]
    assert process.turn_events() == [("turn-1", "Stop"), ("turn-2", "Stop")]
    assert [r["lease_id"] for r in await rows(world)] == [lease_id]
    assert process.close_count == 0

    await session_hosts.close_sessions(["chat"], reason="chat_deleted")
    # SDK scopes close in the actor task that opened them; no lease remains.
    assert process.close_count == 1 and process.closed_in is host._task
    assert host.closed and session_hosts.get("chat") is None
    assert not await rows(world)


async def test_chats_never_share_a_host(world):
    await run(world, chat="chat-a", plan=lambda r: r.js("x = a"))
    second = await run(world, chat="chat-b", plan=lambda r: r.js("x"))
    assert second.value == "undefined"
    assert len(world.processes) == 2
    assert session_hosts.get("chat-a") is not session_hosts.get("chat-b")
    assert len(await rows(world)) == 2


async def test_server_change_and_removal_replace_the_host(world):
    await run(world, plan=lambda r: r.js("x = 1"))
    old = session_hosts.get("chat")
    old_lease = old.desktop.lease.lease_id
    changed = await run(world, turn="turn-2", version="v2", plan=lambda r: r.js("x"))
    assert changed.value == "undefined"
    assert old.closed and old.close_reason == "configuration_changed"
    assert world.processes[0].close_count == 1 and len(world.processes) == 2
    assert (await world.store.get(old_lease))["state"] == "released"

    current = session_hosts.get("chat")
    runtime = McpRuntime()
    try:
        await runtime.setup(
            prepared=prepared(world, plugins=False),
            accessor=world.accessor,
            session_id="chat",
            turn_id="turn-3",
        )
    finally:
        await runtime.aclose()
    assert current.closed and current.close_reason == "server_removed"
    assert not await rows(world)


async def test_unhealthy_lease_or_new_runner_epoch_starts_a_fresh_host(world):
    import sqlite3

    await run(world, plan=lambda r: r.js("x = 1"))
    old = session_hosts.get("chat")
    with sqlite3.connect(world.store.path) as db:
        db.execute(
            "UPDATE desktop_leases SET expires_at=0 WHERE lease_id=?",
            (old.desktop.lease.lease_id,),
        )
    await run(world, turn="turn-2", plan=lambda r: r.js("x"))
    assert old.closed and old.close_reason == "unhealthy"
    renewed = session_hosts.get("chat")
    assert renewed is not old and len(world.processes) == 2

    world.manager.epoch = "restarted"
    await run(world, turn="turn-3", plan=lambda r: r.js("x"))
    assert renewed.closed and session_hosts.get("chat") is not renewed
    assert len(world.processes) == 3


async def test_run_cancelled_while_probing_keeps_the_host_reusable(world, monkeypatch):
    await run(world, plan=lambda r: r.js("x = 1"))
    host = session_hosts.get("chat")
    probing = asyncio.Event()
    healthy = host.healthy

    async def stalled_probe():
        probing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(host, "healthy", stalled_probe)
    task = run(world, turn="turn-2")
    await probing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not host.attached and not host.closing and host._idle_timer is not None
    monkeypatch.setattr(host, "healthy", healthy)
    third = await run(world, turn="turn-3", plan=lambda r: r.js("x"))
    assert third.value == "1" and session_hosts.get("chat") is host


async def test_startup_failure_is_skipped_and_leaves_no_host_or_lease(world):
    async def broken(*args, **kwargs):
        raise RuntimeError("spawn failed")

    world.accessor.open_process = broken
    runtime = McpRuntime()
    try:
        await runtime.setup(
            prepared=prepared(world),
            accessor=world.accessor,
            session_id="chat",
            turn_id="turn-1",
        )
        assert [skip["server"] for skip in runtime.skipped] == ["cua_repl"]
        assert runtime.connections == [] and runtime.desktop is None
    finally:
        await runtime.aclose()
    assert session_hosts.get("chat") is None
    assert not await rows(world)


async def test_idle_timeout_closes_connection_and_releases_lease(world, settings):
    settings.COMPUTER_USE_SESSION_IDLE_SECONDS = 0.05
    await run(world, plan=lambda r: r.js("x = 1"))
    host = session_hosts.get("chat")
    lease_id = host.desktop.lease.lease_id
    await asyncio.wait_for(asyncio.shield(host._task), 5)
    assert host.close_reason == "idle"
    assert world.processes[0].close_count == 1
    assert (await world.store.get(lease_id))["state"] == "released"
    assert session_hosts.get("chat") is None


async def test_lease_failure_cancels_the_attached_run_and_closes_the_host(world):
    async def plan(result):
        await result.js("x = 1")
        lease = result.runtime.desktop.lease
        # Exactly what a rejected renewal does (DesktopLease._renew).
        lease.healthy = False
        lease._on_failure()
        await asyncio.sleep(30)
        pytest.fail("the attached run was not interrupted")

    task = run(world, plan=plan)
    with pytest.raises(asyncio.CancelledError):
        await task
    [process] = world.processes
    assert process.close_count == 1
    assert session_hosts.get("chat") is None
    assert not await rows(world)


async def test_recordings_stay_per_run(world):
    runtime = world.manager._get_runtime(world.workspace)
    runtime.probe_managed_token = AsyncMock(
        return_value={"boot_id": "boot", "init_starttime": "1"}
    )
    runtime.spawn_process = AsyncMock(return_value=object())
    runtime.process_wait = AsyncMock(return_value=0)
    runtime.close_managed_process = AsyncMock(return_value=True)
    runtime.process_close = AsyncMock(return_value=True)
    world.manager._get_desktop_geometry = AsyncMock(return_value=(1280, 720))
    options = {"enable_recording": True}
    first = await run(world, options=options, plan=lambda r: r.js("x = 1"))
    second = await run(world, turn="turn-2", options=options, plan=lambda r: r.js("x"))
    assert len(first.recordings) == len(second.recordings) == 1
    assert first.recordings[0].startswith("/workspace/.opencuria/computeruse/turn-1/")
    assert second.recordings[0].startswith("/workspace/.opencuria/computeruse/turn-2/")
    lease_id = session_hosts.get("chat").desktop.lease.lease_id
    recorded = await world.store.recordings(lease_id)
    assert {row["path"] for row in recorded} == {
        *first.recordings,
        *second.recordings,
    }
    assert all(row["state"] == "closed" for row in recorded)


async def test_desktop_stop_only_interrupts_and_the_repl_continues(world):
    async def plan(result):
        await result.js("x = 7")
        assert not (await result.desktop("stop"))["own_claim"]
        assert (await result.desktop("start"))["own_claim"]
        return await result.js("x")

    finished = await run(world, plan=plan)
    assert finished.value == "7"
    assert world.processes[0].turn_events() == [
        ("turn-1", "DesktopStop"),
        ("turn-1", "Stop"),
    ]
    assert session_hosts.get("chat") is not None


async def test_concurrent_second_run_of_a_chat_gets_its_own_connection(world):
    attached, proceed = asyncio.Event(), asyncio.Event()

    async def hold(result):
        await result.js("x = first")
        attached.set()
        await proceed.wait()

    first = run(world, plan=hold)
    await attached.wait()
    host = session_hosts.get("chat")
    second = await run(world, turn="turn-2", plan=lambda r: r.js("x"))
    assert second.value == "undefined"
    assert len(world.processes) == 2 and world.processes[1].close_count == 1
    assert host.attached and session_hosts.get("chat") is host
    proceed.set()
    await first
    assert not host.attached and world.processes[0].close_count == 0
    assert [r["lease_id"] for r in await rows(world)] == [host.desktop.lease.lease_id]


async def test_runner_stream_failure_closes_matching_hosts(world, monkeypatch):
    from apps.harness.access import runner_accessor

    monkeypatch.setattr(runner_accessor, "_ACCESSORS_BY_STREAM", {})
    await run(world, plan=lambda r: r.js("x = 1"))
    host = session_hosts.get("chat")
    RunnerHarnessGateway().fail_streams(lambda ws: ws != str(world.workspace), "x")
    await asyncio.sleep(0)
    assert not host.closing
    RunnerHarnessGateway().fail_streams(lambda ws: ws == str(world.workspace), "x")
    await asyncio.wait_for(asyncio.shield(host._task), 5)
    assert host.close_reason == "streams_failed"
    assert not await rows(world)


@pytest.mark.django_db(transaction=True)
async def test_chat_deletion_closes_hosts_of_the_chat_and_its_subagents(
    harness_workspace, monkeypatch
):
    from asgiref.sync import sync_to_async

    from apps.harness.harness_service import HarnessService
    from apps.harness.models import HarnessSession

    def create(parent=None):
        return HarnessSession.objects.create(
            workspace=harness_workspace,
            organization_id=harness_workspace.runner.organization_id,
            created_by=harness_workspace.created_by,
            parent=parent,
        )

    root = await sync_to_async(create)()
    child = await sync_to_async(create)(root)
    closed = []

    async def close_sessions(session_ids, *, reason):
        closed.append(({str(value) for value in session_ids}, reason))

    async def emit(event, data):
        return None

    monkeypatch.setattr(session_hosts, "close_sessions", close_sessions)
    await HarnessService(emit=emit).delete_session(root.id)
    assert closed == [({str(root.id), str(child.id)}, "chat_deleted")]


async def test_shutdown_closes_every_host(monkeypatch):
    from apps.harness.harness_service import HarnessService

    reasons = []

    async def close_all(*, reason):
        reasons.append(reason)

    monkeypatch.setattr(session_hosts, "close_all", close_all)
    await HarnessService().shutdown_runs()
    assert reasons == ["shutdown"]
