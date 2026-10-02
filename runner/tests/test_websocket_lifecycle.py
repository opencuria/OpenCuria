"""Lifecycle tests for confirmed runner sessions and session-owned work."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import unittest
from collections import defaultdict
from typing import Any
from unittest.mock import AsyncMock

import socketio
from aiohttp import web
from pydantic import ValidationError
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.interfaces.websocket_lifecycle import (
    PROTOCOL_VERSION,
    ConnectionSupervisor,
    FatalConnectionError,
    RunnerSession,
    SessionLostError,
    cancel_tasks,
)


class FakeEngineIO:
    def __init__(self) -> None:
        self.state = "disconnected"
        self.read_loop_task = None
        self.write_loop_task = None

    async def disconnect(self, abort: bool = False) -> None:
        self.state = "disconnected"


class FakeClient:
    """Minimal AsyncClient contract, with observable transport/session calls."""

    def __init__(self, *, connect_error=None, rejection=None, replies=None) -> None:
        self.connected = False
        self.eio = FakeEngineIO()
        self.handlers: dict[str, Any] = {}
        self.connect_error = connect_error
        self.rejection = rejection
        self.replies = defaultdict(list, replies or {})
        self.calls: list[tuple[str, dict, float]] = []
        self.connect_calls: list[dict] = []
        self.reconnection = False
        self.sid = f"sid-{id(self)}"

    def event(self, handler):
        self.handlers[handler.__name__] = handler
        return handler

    async def connect(self, url: str, **kwargs: Any) -> None:
        self.connect_calls.append({"url": url, **kwargs})
        if self.connect_error is not None:
            error, self.connect_error = self.connect_error, None
            if self.rejection is not None:
                await self.handlers["connect_error"]({"data": self.rejection})
            raise error
        self.connected = True
        self.eio.state = "connected"
        await self.handlers["connect"]()

    async def call(self, event: str, data: dict, timeout: float) -> Any:
        self.calls.append((event, data, timeout))
        if self.replies[event]:
            reply = self.replies[event].pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        return {"ok": True, "sid": self.sid, "protocol_version": PROTOCOL_VERSION}

    def get_sid(self) -> str:
        return self.sid


class DummyService:
    supported_runtimes = ["docker"]


class LifecycleUnitTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **overrides: Any) -> RunnerSettings:
        values = {
            "backend_url": "http://127.0.0.1:1",
            "api_token": "unit-test-token",
            "connection_timeout": 1,
            "runtime_setup_timeout": 2,
            "cleanup_timeout": 1,
            "reconnect_delay": 0.01,
            "reconnect_delay_max": 0.02,
            "heartbeat_interval": 0.01,
        }
        values.update(overrides)
        return RunnerSettings(**values)

    @staticmethod
    async def append_sid(session: RunnerSession, values: list[str]) -> None:
        values.append(session.client.get_sid())

    async def stop_run(
        self, supervisor: ConnectionSupervisor, run: asyncio.Task
    ) -> None:
        await supervisor.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run, 5)

    async def test_initial_transport_failure_recovers_with_new_retry_owner(
        self,
    ) -> None:
        failed = FakeClient(
            connect_error=socketio.exceptions.ConnectionError("offline")
        )
        healthy = FakeClient()
        clients = [failed, healthy]
        created: list[FakeClient] = []
        prepared: list[str] = []
        activated: list[str] = []
        cleaned: list[str] = []

        def create_client() -> FakeClient:
            client = clients.pop(0)
            created.append(client)
            return client

        async def prepare(session: RunnerSession) -> list[dict]:
            prepared.append(session.client.get_sid())
            return [{"workspace_id": "session-snapshot"}]

        async def activate(session: RunnerSession, snapshot: list[dict]) -> None:
            self.assertEqual(snapshot, [{"workspace_id": "session-snapshot"}])
            activated.append(session.client.get_sid())

        supervisor = ConnectionSupervisor(
            self.settings(),
            create_client,
            prepare,
            activate,
            lambda session: self.append_sid(session, cleaned),
            ["docker"],
        )
        run = asyncio.create_task(supervisor.run())
        try:

            async def wait_until_active() -> None:
                while not activated:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_until_active(), 5)
        finally:
            await self.stop_run(supervisor, run)

        self.assertEqual(len(created), 2)
        self.assertTrue(all(client.reconnection is False for client in created))
        self.assertEqual(prepared, [healthy.get_sid()])
        self.assertEqual(activated, [healthy.get_sid()])
        self.assertEqual(cleaned, [failed.get_sid(), healthy.get_sid()])
        self.assertEqual(
            healthy.connect_calls[0]["headers"]["Authorization"],
            "Bearer unit-test-token",
        )
        self.assertEqual(healthy.connect_calls[0]["auth"]["protocol_version"], 1)

    async def test_registration_ack_must_match_session_sid_and_protocol(self) -> None:
        client = FakeClient(
            replies={
                "runner:register": [
                    {
                        "ok": True,
                        "sid": "other-session",
                        "protocol_version": 1,
                    }
                ]
            }
        )
        prepared: list[str] = []
        activated: list[str] = []
        cleaned: list[str] = []
        supervisor = ConnectionSupervisor(
            self.settings(),
            lambda: client,
            lambda session: self._prepare_record(session, prepared),
            lambda session, _snapshot: self.append_sid(session, activated),
            lambda session: self.append_sid(session, cleaned),
            ["docker"],
        )
        with self.assertRaisesRegex(FatalConnectionError, "invalid_protocol_ack"):
            await supervisor.run()
        self.assertEqual(prepared, [client.get_sid()])
        self.assertEqual(activated, [])
        self.assertEqual(cleaned, [client.get_sid()])
        self.assertEqual([name for name, _, _ in client.calls], ["runner:register"])

    @staticmethod
    async def _prepare_record(session: RunnerSession, values: list[str]) -> list[dict]:
        values.append(session.client.get_sid())
        return []

    async def test_registration_timeout_retries_without_activating_unconfirmed_session(
        self,
    ) -> None:
        missing_ack = FakeClient(
            replies={
                "runner:register": [asyncio.TimeoutError("ACK missing")],
            }
        )
        accepted = FakeClient()
        clients = [missing_ack, accepted]
        activated: list[str] = []
        cleaned: list[str] = []
        supervisor = ConnectionSupervisor(
            self.settings(),
            lambda: clients.pop(0),
            AsyncMock(return_value=[]),
            lambda session, _snapshot: self.append_sid(session, activated),
            lambda session: self.append_sid(session, cleaned),
            ["docker"],
        )
        run = asyncio.create_task(supervisor.run())
        try:

            async def wait_for_activation() -> None:
                while not activated:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_for_activation(), 5)
        finally:
            await self.stop_run(supervisor, run)
        self.assertEqual(activated, [accepted.get_sid()])
        self.assertEqual(cleaned, [missing_ack.get_sid(), accepted.get_sid()])
        self.assertEqual(
            [name for name, _, _ in missing_ack.calls], ["runner:register"]
        )

    async def test_heartbeat_timeout_drops_session_and_reconnects(self) -> None:
        timed_out = FakeClient(
            replies={
                "runner:heartbeat": [asyncio.TimeoutError("no liveness ACK")],
            }
        )
        recovered = FakeClient()
        clients = [timed_out, recovered]
        activated: list[str] = []
        cleaned: list[str] = []
        supervisor = ConnectionSupervisor(
            self.settings(),
            lambda: clients.pop(0),
            AsyncMock(return_value=[]),
            lambda session, _snapshot: self.append_sid(session, activated),
            lambda session: self.append_sid(session, cleaned),
            ["docker"],
        )
        run = asyncio.create_task(supervisor.run())
        try:

            async def wait_for_recovery() -> None:
                while len(activated) < 2:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_for_recovery(), 5)
        finally:
            await self.stop_run(supervisor, run)
        self.assertEqual(activated, [timed_out.get_sid(), recovered.get_sid()])
        self.assertIn(
            ("runner:heartbeat", {}, self.settings().connection_timeout),
            timed_out.calls,
        )
        self.assertEqual(cleaned, [timed_out.get_sid(), recovered.get_sid()])

    async def test_disconnect_during_preparation_discards_result_and_retries(
        self,
    ) -> None:
        clients = [FakeClient(), FakeClient()]
        created: list[FakeClient] = []
        prepare_started = asyncio.Event()
        release_prepare = asyncio.Event()
        cleaned: list[str] = []
        activated: list[tuple[str, list[dict]]] = []
        prepare_count = 0

        def create_client() -> FakeClient:
            client = clients.pop(0)
            created.append(client)
            return client

        async def prepare(session: RunnerSession) -> list[dict]:
            nonlocal prepare_count
            prepare_count += 1
            if prepare_count == 1:
                prepare_started.set()
                await release_prepare.wait()
                return [{"stale": True}]
            return [{"sid": session.client.get_sid()}]

        async def activate(session: RunnerSession, snapshot: list[dict]) -> None:
            activated.append((session.client.get_sid(), snapshot))

        supervisor = ConnectionSupervisor(
            self.settings(),
            create_client,
            prepare,
            activate,
            lambda session: self.append_sid(session, cleaned),
            ["docker"],
        )
        run = asyncio.create_task(supervisor.run())
        try:
            await asyncio.wait_for(prepare_started.wait(), 5)
            await created[0].handlers["disconnect"]("transport closed")
            release_prepare.set()

            async def wait_for_recovery() -> None:
                while not activated:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_for_recovery(), 5)
        finally:
            release_prepare.set()
            await self.stop_run(supervisor, run)
        self.assertEqual(
            activated, [(created[1].get_sid(), [{"sid": created[1].get_sid()}])]
        )
        self.assertNotIn((created[0].get_sid(), [{"stale": True}]), activated)
        self.assertEqual(cleaned, [client.get_sid() for client in created])

    async def test_transport_flags_must_be_consistent(self) -> None:
        client = FakeClient()
        client.connected = True
        client.eio.state = "disconnected"
        with self.assertRaises(SessionLostError):
            RunnerSession(client).require_transport()  # type: ignore[arg-type]

    async def test_stop_interrupts_backoff_and_blocked_connect(self) -> None:
        backoff_client = FakeClient(
            connect_error=socketio.exceptions.ConnectionError("offline")
        )
        cleaned: list[str] = []
        supervisor = ConnectionSupervisor(
            self.settings(reconnect_delay=2, reconnect_delay_max=2),
            lambda: backoff_client,
            AsyncMock(return_value=[]),
            AsyncMock(),
            lambda session: self.append_sid(session, cleaned),
            ["docker"],
        )
        run = asyncio.create_task(supervisor.run())
        try:

            async def waited_for_cleanup() -> None:
                while not cleaned:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(waited_for_cleanup(), 5)
        finally:
            await self.stop_run(supervisor, run)
        self.assertEqual(cleaned, [backoff_client.get_sid()])

        class BlockedClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.connect_started = asyncio.Event()

            async def connect(self, url: str, **kwargs: Any) -> None:
                self.connect_started.set()
                await asyncio.Event().wait()

        blocked = BlockedClient()
        cleaned.clear()
        supervisor = ConnectionSupervisor(
            self.settings(),
            lambda: blocked,
            AsyncMock(return_value=[]),
            AsyncMock(),
            lambda session: self.append_sid(session, cleaned),
            ["docker"],
        )
        run = asyncio.create_task(supervisor.run())
        try:
            await asyncio.wait_for(blocked.connect_started.wait(), 5)
        finally:
            await self.stop_run(supervisor, run)
        self.assertEqual(cleaned, [blocked.get_sid()])

    async def test_cancel_tasks_fails_bounded_when_child_suppresses_cancel(
        self,
    ) -> None:
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def stubborn() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        try:
            with self.assertRaisesRegex(
                FatalConnectionError, "session_cleanup_timeout"
            ):
                await cancel_tasks([task], timeout=0.01)
            self.assertTrue(cancelled.is_set())
        finally:
            release.set()
            await asyncio.wait_for(task, 0.5)

    async def test_settings_require_positive_timeouts_and_valid_retry_bounds(
        self,
    ) -> None:
        for field in (
            "connection_timeout",
            "runtime_setup_timeout",
            "cleanup_timeout",
            "reconnect_delay",
            "reconnect_delay_max",
            "heartbeat_interval",
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.settings(**{field: 0})
        with self.assertRaises(ValidationError):
            self.settings(reconnect_delay=0.2, reconnect_delay_max=0.1)
        with self.assertRaises(ValidationError):
            self.settings(backend_url="ftp://localhost")


class RealSocketIOSupervisorTests(unittest.IsolatedAsyncioTestCase):
    """In-process transport integration; uses no backend, DB or runtime."""

    async def asyncSetUp(self) -> None:
        self.server = socketio.AsyncServer(async_mode="aiohttp")
        self.app = web.Application()
        self.server.attach(self.app, socketio_path="ws/runner")
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.settings = RunnerSettings(
            backend_url=f"http://127.0.0.1:{port}",
            socketio_path="/ws/runner",
            api_token="local-test-token",
            connection_timeout=1,
            runtime_setup_timeout=2,
            cleanup_timeout=1,
            reconnect_delay=0.01,
            reconnect_delay_max=0.02,
            heartbeat_interval=0.02,
        )

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    @staticmethod
    def service() -> DummyService:
        service = DummyService()
        service.sync_from_runtime = AsyncMock()
        service.recover_desktop_sessions_from_runtime = AsyncMock()
        service.get_workspace_heartbeat_statuses = AsyncMock(return_value=[])
        service.run_health_check_loop = AsyncMock(side_effect=asyncio.Event().wait)
        return service

    async def test_auth_register_heartbeat_protocol_and_health_check(self) -> None:
        authenticated: list[dict] = []
        registrations: list[tuple[str, dict]] = []
        heartbeat = asyncio.Event()

        @self.server.event
        async def connect(sid, environ, auth):
            authenticated.append(auth)
            return auth.get("token") == "local-test-token"

        @self.server.on("runner:register")
        async def register(sid, data):
            registrations.append((sid, data))
            return {"ok": True, "sid": sid, "runner_id": "local", "protocol_version": 1}

        @self.server.on("runner:heartbeat")
        async def on_heartbeat(sid, data):
            self.assertEqual(data, {})
            heartbeat.set()
            return {"ok": True, "sid": sid, "protocol_version": 1}

        service = self.service()
        interface = WebSocketInterface(service, self.settings)
        task = asyncio.create_task(interface.start())
        try:
            await asyncio.wait_for(heartbeat.wait(), 5)
            self.assertEqual(authenticated[0]["protocol_version"], 1)
            self.assertEqual(authenticated[0]["token"], "local-test-token")
            self.assertEqual(registrations[0][1]["protocol_version"], 1)
            self.assertEqual(registrations[0][1]["status"], "ready")
            service.run_health_check_loop.assert_awaited_once()
        finally:
            await interface.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_explicit_invalid_auth_is_fatal_and_does_not_retry(self) -> None:
        registration_count = 0
        connection_count = 0

        @self.server.event
        async def connect(sid, environ, auth):
            nonlocal connection_count
            connection_count += 1
            raise socketio.exceptions.ConnectionRefusedError(
                "Invalid API token",
                {"code": "invalid_token", "retryable": False},
            )

        @self.server.on("runner:register")
        async def register(sid, data):
            nonlocal registration_count
            registration_count += 1
            return {"ok": True, "sid": sid, "protocol_version": 1}

        service = self.service()
        interface = WebSocketInterface(service, self.settings)
        task = asyncio.create_task(interface.start())
        try:
            with self.assertRaisesRegex(FatalConnectionError, "invalid_token"):
                await asyncio.wait_for(task, 5)
            self.assertEqual(connection_count, 1)
            self.assertEqual(registration_count, 0)
            service.sync_from_runtime.assert_not_awaited()
        finally:
            await interface.stop()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def test_disconnect_during_blocked_preparation_recovers_on_fresh_session(
        self,
    ) -> None:
        """Dropping the real transport discards the blocked session's snapshot."""
        connections: list[str] = []
        registrations: list[str] = []
        preparation_started = asyncio.Event()
        release_prepare = asyncio.Event()
        preparation_calls = 0
        emitted_snapshots: list[tuple[str, list[dict]]] = []
        snapshot_sent = asyncio.Event()

        @self.server.event
        async def connect(sid, environ, auth):
            connections.append(sid)
            return True

        @self.server.on("runner:register")
        async def register(sid, data):
            registrations.append(sid)
            return {"ok": True, "sid": sid, "protocol_version": 1}

        @self.server.on("runner:heartbeat")
        async def on_heartbeat(sid, data):
            return {"ok": True, "sid": sid, "protocol_version": 1}

        service = self.service()

        async def blocked_sync() -> None:
            nonlocal preparation_calls
            preparation_calls += 1
            if preparation_calls == 1:
                preparation_started.set()
                await release_prepare.wait()

        service.sync_from_runtime = AsyncMock(side_effect=blocked_sync)
        service.get_workspace_heartbeat_statuses = AsyncMock(
            side_effect=lambda: (
                [{"stale": True}] if preparation_calls == 1 else [{"fresh": True}]
            )
        )
        interface = WebSocketInterface(service, self.settings)
        original_status_loop = interface._status_loop

        async def capture_status(sio, initial_snapshot: list[dict]) -> None:
            emitted_snapshots.append((sio.get_sid(), initial_snapshot))
            snapshot_sent.set()
            await original_status_loop(sio, initial_snapshot)

        interface._status_loop = capture_status
        task = asyncio.create_task(interface.start())
        try:
            await asyncio.wait_for(preparation_started.wait(), 5)
            await self.server.disconnect(connections[0])
            release_prepare.set()
            await asyncio.wait_for(snapshot_sent.wait(), 5)
            self.assertEqual(len(connections), 2)
            self.assertEqual(len(registrations), 1)
            self.assertEqual(emitted_snapshots, [(connections[1], [{"fresh": True}])])
        finally:
            release_prepare.set()
            await interface.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_status_runtime_sync_can_block_without_starving_heartbeat(
        self,
    ) -> None:
        """Status refresh stalls in runtime sync, but confirmed liveness proceeds."""
        registered = asyncio.Event()
        heartbeat_count = 0
        status_sync_started = asyncio.Event()
        release_status_sync = asyncio.Event()

        @self.server.event
        async def connect(sid, environ, auth):
            return True

        @self.server.on("runner:register")
        async def register(sid, data):
            registered.set()
            return {"ok": True, "sid": sid, "protocol_version": 1}

        @self.server.on("runner:heartbeat")
        async def on_heartbeat(sid, data):
            nonlocal heartbeat_count
            heartbeat_count += 1
            return {"ok": True, "sid": sid, "protocol_version": 1}

        service = self.service()

        async def sync_from_runtime() -> None:
            # The first call is connection preparation. Block only status refresh.
            if service.sync_from_runtime.await_count > 1:
                status_sync_started.set()
                await release_status_sync.wait()

        service.sync_from_runtime = AsyncMock(side_effect=sync_from_runtime)
        interface = WebSocketInterface(service, self.settings)
        task = asyncio.create_task(interface.start())
        try:
            await asyncio.wait_for(registered.wait(), 5)
            await asyncio.wait_for(status_sync_started.wait(), 5)
            before = heartbeat_count

            async def several_heartbeats() -> None:
                while heartbeat_count < before + 2:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(several_heartbeats(), 5)
            self.assertTrue(status_sync_started.is_set())
        finally:
            release_status_sync.set()
            await interface.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_mutating_handler_is_drained_before_reconnect(self) -> None:
        """A disconnected create finishes once and cannot emit via its successor."""
        connections: list[str] = []
        registrations: list[str] = []
        output_attempts: list[tuple[str, str]] = []
        operation_started = asyncio.Event()
        release_operation = asyncio.Event()
        operation_cancelled = asyncio.Event()
        operation_finished = asyncio.Event()

        @self.server.event
        async def connect(sid, environ, auth):
            connections.append(sid)
            return True

        @self.server.on("runner:register")
        async def register(sid, data):
            registrations.append(sid)
            return {"ok": True, "sid": sid, "protocol_version": 1}

        @self.server.on("runner:heartbeat")
        async def on_heartbeat(sid, data):
            return {"ok": True, "sid": sid, "protocol_version": 1}

        service = self.service()
        service.streams = None

        async def create_workspace(**_kwargs):
            operation_started.set()
            try:
                await release_operation.wait()
            except asyncio.CancelledError:
                operation_cancelled.set()
                raise
            operation_finished.set()
            return "workspace-created", False

        service.create_workspace = AsyncMock(side_effect=create_workspace)
        interface = WebSocketInterface(service, self.settings)
        original_factory = interface._create_client
        client_ids: dict[int, str] = {}

        def make_observed_client():
            client = original_factory()
            client_id = f"client-{len(client_ids) + 1}"
            client_ids[id(client)] = client_id
            original_emit = client.emit

            async def observed_emit(event: str, data=None, *args, **kwargs):
                if event in {"workspace:created", "workspace:error"}:
                    output_attempts.append((client_id, event))
                return await original_emit(event, data=data, *args, **kwargs)

            client.emit = observed_emit
            return client

        interface._create_client = make_observed_client
        interface._supervisor.create_client = make_observed_client
        task = asyncio.create_task(interface.start())
        try:

            async def wait_for_registration(count: int) -> None:
                while len(registrations) < count:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_for_registration(1), 5)
            old_sid = registrations[0]
            old_client_id = client_ids[id(interface._sio)]
            await self.server.emit(
                "task:create_workspace",
                {"task_id": "create-once", "repos": [], "runtime_type": "docker"},
                to=connections[0],
            )
            await asyncio.wait_for(operation_started.wait(), 5)
            await self.server.disconnect(old_sid)
            await asyncio.sleep(0.04)
            self.assertFalse(operation_cancelled.is_set())
            self.assertEqual(len(registrations), 1)

            release_operation.set()
            await asyncio.wait_for(operation_finished.wait(), 5)
            await asyncio.wait_for(wait_for_registration(2), 5)
            self.assertFalse(operation_cancelled.is_set())
            self.assertEqual(service.create_workspace.await_count, 1)
            self.assertNotEqual(registrations[0], registrations[1])
            self.assertIn((old_client_id, "workspace:created"), output_attempts)
            self.assertNotIn(
                (client_ids[id(interface._sio)], "workspace:created"),
                output_attempts,
            )
        finally:
            release_operation.set()
            await interface.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_late_terminal_start_cancels_new_reader_before_reconnect(
        self,
    ) -> None:
        """A late PTY start is drained, then its reader is closed before retry."""
        connections: list[str] = []
        registrations: list[str] = []
        start_started = asyncio.Event()
        release_start = asyncio.Event()
        reader_started = asyncio.Event()
        reader_exited = asyncio.Event()
        emit_attempts: list[tuple[str, str]] = []

        @self.server.event
        async def connect(sid, environ, auth):
            connections.append(sid)
            return True

        @self.server.on("runner:register")
        async def register(sid, data):
            registrations.append(sid)
            return {"ok": True, "sid": sid, "protocol_version": 1}

        @self.server.on("runner:heartbeat")
        async def on_heartbeat(sid, data):
            return {"ok": True, "sid": sid, "protocol_version": 1}

        service = self.service()
        service.streams = None

        async def start_terminal(_workspace_id, *, cols, rows):
            start_started.set()
            await release_start.wait()
            return "late-pty-id"

        async def read_terminal(_terminal_id):
            reader_started.set()
            try:
                await asyncio.Event().wait()
                yield b"never emitted"
            finally:
                reader_exited.set()

        terminal_manager = type("TerminalManager", (), {})()
        terminal_manager.start_terminal = AsyncMock(side_effect=start_terminal)
        terminal_manager.read_terminal = read_terminal
        terminal_manager.close_terminal = AsyncMock()
        service.terminal_manager = terminal_manager

        interface = WebSocketInterface(service, self.settings)
        original_factory = interface._create_client
        client_ids: dict[int, str] = {}

        def create_client_with_emit_tracking():
            client = original_factory()
            client_id = f"client-{len(client_ids) + 1}"
            client_ids[id(client)] = client_id
            original_emit = client.emit

            async def observed_emit(event: str, data=None, *args, **kwargs):
                if event.startswith("terminal:"):
                    emit_attempts.append((client_id, event))
                    if not client.connected:
                        return None
                return await original_emit(event, data=data, *args, **kwargs)

            client.emit = observed_emit
            return client

        interface._create_client = create_client_with_emit_tracking
        interface._supervisor.create_client = create_client_with_emit_tracking
        task = asyncio.create_task(interface.start())
        try:

            async def wait_for_registrations(count: int) -> None:
                while len(registrations) < count:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_for_registrations(1), 5)
            old_sid = registrations[0]
            old_client_id = client_ids[id(interface._sio)]
            await self.server.emit(
                "task:start_terminal",
                {
                    "task_id": "late-terminal",
                    "workspace_id": "00000000-0000-0000-0000-000000000001",
                    "cols": 90,
                    "rows": 30,
                },
                to=connections[0],
            )
            await asyncio.wait_for(start_started.wait(), 5)
            await self.server.disconnect(old_sid)
            await asyncio.sleep(0.03)
            self.assertEqual(len(registrations), 1)
            release_start.set()
            await asyncio.wait_for(reader_started.wait(), 5)
            await asyncio.wait_for(reader_exited.wait(), 5)
            terminal_manager.start_terminal.assert_awaited_once_with(
                unittest.mock.ANY, cols=90, rows=30
            )
            terminal_manager.close_terminal.assert_awaited_once_with("late-pty-id")
            await asyncio.wait_for(wait_for_registrations(2), 5)

            self.assertNotEqual(registrations[0], registrations[1])
            self.assertEqual(interface._running_tasks, {})
            self.assertEqual(interface._mutation_tasks, set())
            self.assertIn((old_client_id, "terminal:started"), emit_attempts)
            self.assertIn((old_client_id, "terminal:closed"), emit_attempts)
            successor_id = client_ids[id(interface._sio)]
            self.assertFalse(
                any(client_id == successor_id for client_id, _ in emit_attempts)
            )
        finally:
            release_start.set()
            await interface.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_mutation_drain_timeout_is_fatal_and_never_starts_successor(
        self,
    ) -> None:
        """A stuck runtime mutation fails closed rather than overlapping retries."""
        connections: list[str] = []
        registrations: list[str] = []
        mutation_started = asyncio.Event()
        release_mutation = asyncio.Event()

        @self.server.event
        async def connect(sid, environ, auth):
            connections.append(sid)
            return True

        @self.server.on("runner:register")
        async def register(sid, data):
            registrations.append(sid)
            return {"ok": True, "sid": sid, "protocol_version": 1}

        service = self.service()
        service.streams = None

        async def create_workspace(**_kwargs):
            mutation_started.set()
            await release_mutation.wait()
            return "created", False

        service.create_workspace = AsyncMock(side_effect=create_workspace)
        settings = self.settings.model_copy(update={"runtime_setup_timeout": 0.04})
        interface = WebSocketInterface(service, settings)
        task = asyncio.create_task(interface.start())
        try:

            async def wait_for_registration() -> None:
                while not registrations:
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_for_registration(), 5)
            await self.server.emit(
                "task:create_workspace",
                {"task_id": "stuck-create", "repos": []},
                to=connections[0],
            )
            await asyncio.wait_for(mutation_started.wait(), 5)
            await self.server.disconnect(connections[0])
            with self.assertRaisesRegex(
                FatalConnectionError, "runtime_mutation_drain_timeout"
            ):
                await asyncio.wait_for(task, 5)
            self.assertEqual(len(registrations), 1)
        finally:
            release_mutation.set()
            await interface.stop()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.sleep(0)

    async def test_health_check_loop_remains_alive_across_initial_connection_outage(
        self,
    ) -> None:
        # Start with a refused local port, then prove the independent health loop
        # is still alive while the supervisor has not established any session.
        service = self.service()
        unused = socket.socket()
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()
        settings = self.settings.model_copy(
            update={"backend_url": f"http://127.0.0.1:{port}"}
        )
        interface = WebSocketInterface(service, settings)
        task = asyncio.create_task(interface.start())
        try:
            await asyncio.sleep(0.06)
            service.run_health_check_loop.assert_awaited_once()
            self.assertIsNotNone(interface._health_check_task)
            self.assertFalse(interface._health_check_task.done())
        finally:
            await interface.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertIsNone(interface._health_check_task)


if __name__ == "__main__":
    unittest.main()
