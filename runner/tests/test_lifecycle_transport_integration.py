"""Real loopback Socket.IO outages with the production runner service/executor."""

import asyncio
import socket
import uuid

import pytest
import socketio
from aiohttp import web

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.runtime.base import RuntimeWorkspaceInfo
from src.runtime.inventory import RuntimeInventory
from src.service import WorkspaceService


class LocalRuntime:
    """Isolated guest substitute; lifecycle orchestration uses real managers."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.workspace_id = ""

    async def create_workspace(self, config) -> str:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        self.workspace_id = config.workspace_id
        return self.workspace_id

    async def list_workspaces(self) -> list[RuntimeWorkspaceInfo]:
        if not self.workspace_id:
            return []
        return [
            RuntimeWorkspaceInfo(self.workspace_id, self.workspace_id, "running")
        ]

    async def workspace_incarnation(self, workspace_id: str):
        if workspace_id != self.workspace_id:
            return None
        return {"domain_uuid": "isolated-test-domain"}, "running"

    async def inventory(self) -> RuntimeInventory:
        return RuntimeInventory(complete=True)

    async def exec_command_wait(self, instance_id, **kwargs):
        # Exercise the real credential manager's empty-secret scrub operation.
        assert instance_id == self.workspace_id
        return 0, ""


@pytest.mark.asyncio
async def test_wire_outage_keeps_lifecycle_and_replays_durable_terminal_once(tmp_path):
    """Startup outage, duplicate delivery, server restart and durable result ACK."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    runtime = LocalRuntime()
    settings = RunnerSettings(
        backend_url=f"http://127.0.0.1:{port}", state_dir=str(tmp_path),
        inventory_interval=3600,
    )
    # Short ticks keep the integration test bounded without replacing clocks or
    # bypassing actual network connection/reconnection behavior.
    settings.heartbeat_interval = 0.05
    settings.desktop_lease_interval = 0.05
    service = WorkspaceService(runtime, settings)
    interface = WebSocketInterface(service, settings)
    interface._sio.reconnection_delay = 0.05
    interface._sio.reconnection_delay_max = 0.1
    interface._sio.randomization_factor = 0
    initial_failure = asyncio.Event()
    disconnected = asyncio.Event()
    acknowledged = asyncio.Event()
    registered = asyncio.Queue()
    applied: dict[str, dict] = {}
    received: list[dict] = []
    servers: list[tuple[socketio.AsyncServer, web.AppRunner]] = []

    original_connect = interface._sio.connect

    async def connect(*args, **kwargs):
        try:
            return await original_connect(*args, **kwargs)
        except socketio.exceptions.ConnectionError:
            initial_failure.set()
            raise

    interface._sio.connect = connect
    original_disconnect = interface._sio.handlers["/"]["disconnect"]

    async def on_disconnect(*args):
        await original_disconnect()
        disconnected.set()

    interface._sio.on("disconnect", on_disconnect)
    original_ack = interface._operations.acknowledge

    async def on_ack(data):
        await original_ack(data)
        acknowledged.set()

    interface._sio.on("operation:ack", on_ack)

    async def start_backend() -> socketio.AsyncServer:
        server = socketio.AsyncServer(async_mode="aiohttp")

        @server.on("runner:register")
        async def register(sid, data):
            await registered.put(sid)

        @server.on("operation:result")
        async def result(sid, envelope):
            received.append(envelope)
            data = envelope["data"]
            # Backend persistence is idempotent even if ACK and replay overlap.
            applied.setdefault(data["operation_id"], envelope)
            await server.emit(
                "operation:ack",
                {key: data[key] for key in (
                    "operation_id", "attempt", "target", "runner_id",
                )},
                to=sid,
            )

        app = web.Application()
        server.attach(app, socketio_path=settings.socketio_path)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        servers.append((server, runner))
        return server

    async def stop_backend() -> None:
        server, runner = servers.pop()
        if server.eio.sockets:
            await server.eio.disconnect()
        await server.shutdown()
        await runner.cleanup()

    running = asyncio.create_task(interface.start())
    try:
        # The first attempt really targets an unopened loopback TCP port.
        await asyncio.wait_for(initial_failure.wait(), 5)
        backend = await start_backend()
        sid = await asyncio.wait_for(registered.get(), 8)
        workspace_id = str(uuid.uuid4())
        payload = {
            "task_id": "wire-create", "operation_id": "wire-create",
            "attempt": 1, "runner_id": "isolated-runner", "target": workspace_id,
            "workspace_id": workspace_id, "repos": [],
            "base_image_path": "/isolated-test/base.qcow2",
        }
        await backend.emit("task:create_workspace", payload, to=sid)
        await asyncio.wait_for(runtime.entered.wait(), 5)
        await backend.emit("task:create_workspace", payload, to=sid)
        await stop_backend()
        await asyncio.wait_for(disconnected.wait(), 5)
        assert interface._operations.running

        runtime.release.set()
        await asyncio.wait_for(
            asyncio.gather(*list(interface._operations.running.values())), 5
        )
        pending = await interface._operations.journal.call("pending")
        assert len(pending) == 1
        assert pending[0][0] == "workspace:created"
        assert applied == {}
        assert runtime.calls == 1

        await start_backend()
        await asyncio.wait_for(registered.get(), 8)
        await asyncio.wait_for(acknowledged.wait(), 5)
        assert (await interface._operations.journal.call("pending")) == []
        assert list(applied) == ["wire-create"]
        assert all(item["event"] == "workspace:created" for item in received)
        assert applied["wire-create"]["data"]["execution_finished"] is True
        assert runtime.calls == 1
    finally:
        runtime.release.set()
        await interface.stop()
        await asyncio.wait_for(running, 5)
        while servers:
            await stop_backend()
        interface._operations.journal.close()
