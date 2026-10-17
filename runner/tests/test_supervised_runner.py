"""Runner supervisor and private IPC regression tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.journal import Journal
from src.supervised_ipc import IPCPeer
from src.supervised_worker import WorkerSocket
from src.supervisor import RunnerSupervisor


class SupervisedIPCProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.peers: list[IPCPeer] = []

    async def asyncTearDown(self) -> None:
        for peer in self.peers:
            await peer.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def test_ipc_round_trip_preserves_ack(self) -> None:
        """Private IPC preserves explicit request/ACK semantics."""

        async def accept(reader, writer):
            peer = IPCPeer(
                reader,
                writer,
                request_handler=lambda method, data: asyncio.sleep(
                    0, result={"method": method, "accepted": data["value"]}
                ),
                event_handler=AsyncMock(),
            )
            self.peers.append(peer)
            await peer.start()

        self.server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        client = IPCPeer(
            reader,
            writer,
            request_handler=AsyncMock(),
            event_handler=AsyncMock(),
        )
        self.peers.append(client)
        await client.start()
        value = "a" * 4096
        self.assertEqual(
            await client.call("stream.ack", {"value": value}),
            {"method": "stream.ack", "accepted": value},
        )


class SupervisedArchitectureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = RunnerSettings(state_dir=str(self.root / "state"))
        self.supervisor = RunnerSupervisor(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_terminal_registry_survives_restart_and_targets_new_generation(
        self,
    ) -> None:
        state_dir = str(self.root / "durable-state")
        journal = Journal(state_dir)
        journal.remember_terminal("workspace-1", "terminal-1")
        journal.close()

        reloaded = Journal(state_dir)
        self.supervisor._journal = reloaded
        self.supervisor._active_generation = "new"
        self.supervisor._workers = {"new": SimpleNamespace(active=True, ready=True)}
        self.supervisor._terminal_ids.update(reloaded.active_terminals())
        self.supervisor._terminal_workspaces["terminal-1"] = "workspace-1"
        self.supervisor._pinned_workspaces["workspace-1"] = "new"
        self.assertEqual(
            self.supervisor._owner_for_event(
                "terminal:attach",
                {"workspace_id": "workspace-1", "terminal_id": "terminal-1"},
            ),
            "new",
        )
        reloaded.forget_terminal("workspace-1", "terminal-1")
        reloaded.close()

    async def test_owner_router_pins_terminal_until_close(self) -> None:
        self.supervisor._workers = {
            "old": SimpleNamespace(active=False, ready=True),
            "new": SimpleNamespace(active=True, ready=True),
        }
        self.supervisor._active_generation = "new"
        self.supervisor._terminal_owners["term-1"] = "old"
        self.supervisor._terminal_workspaces["term-1"] = "workspace-1"
        self.assertEqual(
            self.supervisor._owner_for_event(
                "terminal:input", {"terminal_id": "term-1"}
            ),
            "old",
        )
        self.assertEqual(self.supervisor._workspace_owner("workspace-1"), "old")
        self.supervisor._record_session_owner(
            "old", "terminal:closed", {"terminal_id": "term-1"}
        )
        self.assertIsNone(self.supervisor._workspace_owner("workspace-1"))

    def test_worker_adapter_registers_terminal_handlers(self) -> None:
        """Workers expose the shared handler implementation through IPC."""
        from src.interfaces.websocket import WebSocketInterface

        service = SimpleNamespace(
            supported_runtimes=[], capture_fence=None, _runtimes={}, _cache={}
        )
        gateway = WorkerSocket()
        WebSocketInterface(
            service,
            RunnerSettings(state_dir=str(self.root / "worker")),
            sio_client=gateway,
            own_journal=False,
        )
        self.assertIn("task:start_terminal", gateway.handlers["/"])
        self.assertIn("terminal:attach", gateway.handlers["/"])
        self.assertIn("terminal:input", gateway.handlers["/"])


if __name__ == "__main__":
    unittest.main()


async def test_staging_rejects_mutated_source_before_worker_launch(tmp_path):
    """A staged code generation is content-addressed, not a mutable checkout."""
    import pytest

    package = tmp_path / "source" / "src"
    package.mkdir(parents=True)
    (package / "supervised_worker.py").write_text("# initial\n")
    supervisor = RunnerSupervisor(RunnerSettings(state_dir=str(tmp_path / "state")))
    supervisor._stage_source(package.parent, "v1", activate=False)
    staged = supervisor.generations_dir / "v1" / "src" / "supervised_worker.py"
    staged.chmod(0o644)
    staged.write_text("# modified\n")
    with pytest.raises(ValueError, match="changed after staging"):
        await supervisor._activate_generation("v1")


async def test_backend_worker_identity_is_not_runner_generation(tmp_path):
    """Backend run owner and runner executable generation are distinct IDs."""
    import uuid

    supervisor = RunnerSupervisor(RunnerSettings(state_dir=str(tmp_path)))
    supervisor._active_generation = "runner-v2"
    supervisor._workers["runner-v2"] = SimpleNamespace(
        generation_id="runner-v2", ready=True
    )
    supervisor._sio.call = AsyncMock(return_value={"ok": True})
    authority = {
        "run_id": str(uuid.uuid4()),
        "owner_token": str(uuid.uuid4()),
        "root_run_id": str(uuid.uuid4()),
        "worker_id": "backend-worker-old",
        "deployment_version": "backend-v1",
    }
    response = await supervisor._runner_route_request(
        {
            "runner_event": "harness:exec_wait",
            "request": {
                "workspace_id": "workspace",
                "request_id": "req",
                "run_authority": authority,
            },
        }
    )
    assert response["generation"] == "runner-v2"
    assert supervisor._run_owners[authority["run_id"]] == "runner-v2"
    assert supervisor._run_workers[authority["run_id"]] == "backend-worker-old"
    supervisor._active_generation = "runner-v3"
    response = await supervisor._runner_route_request(
        {
            "runner_event": "harness:exec_wait",
            "request": {
                "workspace_id": "workspace",
                "request_id": "next",
                "run_authority": authority,
            },
        }
    )
    assert response["generation"] == "runner-v2"


async def test_stale_run_authority_is_rejected_before_dispatch(tmp_path):
    supervisor = RunnerSupervisor(RunnerSettings(state_dir=str(tmp_path)))
    supervisor._sio.call = AsyncMock(return_value={"ok": False})
    response = await supervisor._runner_route_request(
        {
            "runner_event": "harness:exec_wait",
            "request": {"workspace_id": "workspace", "request_id": "req"},
        }
    )
    assert response["ok"] is False
    assert supervisor._run_owners == {}


async def test_terminal_socket_handler_is_awaitable(tmp_path):
    """Socket.IO must await async handlers, not discard lambda coroutines."""
    import inspect

    supervisor = RunnerSupervisor(RunnerSettings(state_dir=str(tmp_path)))
    supervisor._operations = SimpleNamespace(wrap=lambda event, handler: handler)
    supervisor._register_gateway_handlers()
    for event in (
        "terminal:attach",
        "terminal:input",
        "terminal:resize",
        "terminal:close",
    ):
        assert inspect.iscoroutinefunction(supervisor._sio.handlers["/"][event])


async def test_desktop_resource_does_not_repin_new_agent_run(tmp_path):
    """Old desktop manager may coexist with new exec/run generation safely."""
    import uuid

    supervisor = RunnerSupervisor(RunnerSettings(state_dir=str(tmp_path)))
    supervisor._active_generation = "runner-new"
    supervisor._workers["runner-new"] = SimpleNamespace(
        generation_id="runner-new", ready=True
    )
    supervisor._sio.call = AsyncMock(return_value={"ok": True})
    authority = {
        "run_id": str(uuid.uuid4()),
        "root_run_id": str(uuid.uuid4()),
        "owner_token": str(uuid.uuid4()),
        "worker_id": "backend",
        "deployment_version": "v1",
    }
    supervisor._run_owners[authority["root_run_id"]] = "runner-new"
    supervisor._desktop_workspace_owners["workspace"] = "runner-old"
    response = await supervisor._runner_route_request(
        {
            "runner_event": "harness:desktop_action",
            "request": {
                "workspace_id": "workspace",
                "request_id": "req",
                "action": "renew",
                "run_authority": authority,
            },
        }
    )
    assert response["generation"] == "runner-old"
    assert supervisor._run_owners[authority["root_run_id"]] == "runner-new"
