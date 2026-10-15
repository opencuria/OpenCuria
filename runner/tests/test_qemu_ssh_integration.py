"""Real, localhost-only SSH integration tests for QEMU connection ownership.

These tests exercise AsyncSSH's actual transport and channel lifecycle without
starting a VM or requiring libvirt services. The production connector is
redirected to an ephemeral loopback server; all client SSH credentials and
host keys are generated in pytest's temporary directory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

asyncssh = pytest.importorskip("asyncssh")
pytest.importorskip("libvirt")

from src.runtime import qemu_runtime
from src.runtime.qemu_runtime import QemuRuntime

# Preserve the real connector before the fixture patches only the runtime's
# connect call to route it to the ephemeral server.
_REAL_ASYNCSSH_CONNECT = asyncssh.connect


class _ServerControl:
    """Mutable state shared by the local SSH server and its test sessions."""

    def __init__(self) -> None:
        self.max_sessions: int | None = None
        self.active_sessions = 0
        self.opened_sessions = 0
        self.closed_sessions = 0
        self.refused_sessions = 0
        self.commands: list[str] = []
        self.probe_sessions_started = 0
        self.delay_session_open = False
        self.release_session_open = asyncio.Event()
        self.pending_session_opens = 0
        self.stream_releases = {
            "stream-alpha": asyncio.Event(),
            "stream-beta": asyncio.Event(),
        }
        self.changed = asyncio.Event()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.task_errors: list[BaseException] = []
        self.connections: set[Any] = set()
        self.client_connections: list[Any] = []
        self.connection_count = 0
        self.server_port = 0
        self.port = 0
        self.acceptors: list[Any] = []
        self.relays: list[_TCPRelay] = []

    def notify(self) -> None:
        self.changed.set()

    async def wait_for(self, predicate, *, timeout: float = 1.0) -> None:
        async def wait() -> None:
            while not predicate():
                self.changed.clear()
                # No other coroutine can run between the predicate check and
                # clear; every relevant state change sets this event.
                if not predicate():
                    await self.changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)

    def start_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self.tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    self.task_errors.append(error)

        task.add_done_callback(completed)


class _TCPRelay:
    """Tiny local TCP relay which can blackhole both directions on demand."""

    def __init__(self, destination_port: int) -> None:
        self.destination_port = destination_port
        self.blackhole = False
        self.server: asyncio.AbstractServer | None = None
        self.handlers: set[asyncio.Task[Any]] = set()
        self.writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client_connected, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    async def _handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        backend_writer: asyncio.StreamWriter | None = None
        self.writers.add(client_writer)
        try:
            backend_reader, backend_writer = await asyncio.open_connection(
                "127.0.0.1", self.destination_port
            )
            self.writers.add(backend_writer)

            async def forward(
                source: asyncio.StreamReader, destination: asyncio.StreamWriter
            ) -> None:
                while data := await source.read(65536):
                    if not self.blackhole:
                        destination.write(data)
                        await destination.drain()

            forwards = {
                asyncio.create_task(forward(client_reader, backend_writer)),
                asyncio.create_task(forward(backend_reader, client_writer)),
            }
            try:
                _, pending = await asyncio.wait(
                    forwards, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in forwards:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*forwards, return_exceptions=True)
                del pending
        except (ConnectionError, OSError):
            pass
        finally:
            self.writers.discard(client_writer)
            client_writer.close()
            if backend_writer is not None:
                self.writers.discard(backend_writer)
                backend_writer.close()
            await asyncio.gather(
                client_writer.wait_closed(),
                *([backend_writer.wait_closed()] if backend_writer is not None else []),
                return_exceptions=True,
            )

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        if self.handlers:
            for task in list(self.handlers):
                task.cancel()
            await asyncio.gather(*list(self.handlers), return_exceptions=True)


class _LocalSSHServer(asyncssh.SSHServer):
    def __init__(self, control: _ServerControl) -> None:
        self._control = control
        self._connection_closed = asyncio.Event()

    def connection_made(self, conn) -> None:
        self._conn = conn
        self._control.connections.add(conn)
        self._control.connection_count += 1
        self._control.notify()

    def connection_lost(self, exc) -> None:
        self._connection_closed.set()
        if hasattr(self, "_conn"):
            self._control.connections.discard(self._conn)
        self._control.notify()

    def begin_auth(self, username: str) -> bool:
        # The server binds only to 127.0.0.1 and requires no production
        # credentials. AsyncSSH's client key is still supplied by the runtime.
        del username
        return False

    async def session_requested(self):
        control = self._control
        if (
            control.max_sessions is not None
            and control.active_sessions >= control.max_sessions
        ):
            control.refused_sessions += 1
            control.notify()
            raise asyncssh.ChannelOpenError(
                asyncssh.OPEN_RESOURCE_SHORTAGE, "test MaxSessions limit reached"
            )

        if control.delay_session_open:
            control.pending_session_opens += 1
            control.notify()
            release_task = asyncio.create_task(control.release_session_open.wait())
            closed_task = asyncio.create_task(self._connection_closed.wait())
            try:
                await asyncio.wait(
                    {release_task, closed_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Closing the probe transport must unblock a deliberately
                # delayed session request without opening a late channel.
                if self._connection_closed.is_set():
                    return False
            finally:
                for task in (release_task, closed_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(release_task, closed_task, return_exceptions=True)
                control.pending_session_opens -= 1
                control.notify()

            if (
                control.max_sessions is not None
                and control.active_sessions >= control.max_sessions
            ):
                control.refused_sessions += 1
                control.notify()
                raise asyncssh.ChannelOpenError(
                    asyncssh.OPEN_RESOURCE_SHORTAGE, "test MaxSessions limit reached"
                )

        control.active_sessions += 1
        control.opened_sessions += 1
        control.notify()
        return _LocalSSHSession(control)


class _LocalSSHSession(asyncssh.SSHServerSession):
    def __init__(self, control: _ServerControl) -> None:
        self._control = control
        self._channel = None
        self._command = ""
        self._closed = asyncio.Event()
        self._counted = True

    def connection_made(self, channel) -> None:
        self._channel = channel

    def exec_requested(self, command: str) -> bool:
        self._command = command
        self._control.commands.append(command)
        return True

    def session_started(self) -> None:
        if self._command == "echo ok":
            self._control.probe_sessions_started += 1
            self._control.notify()
        self._control.start_task(self._serve())

    async def _serve(self) -> None:
        try:
            if self._command == "echo ok":
                # Keep health channels open until the owning probe transport
                # closes, forcing check_reachable to be cancelled by its caller.
                await self._closed.wait()
                return

            for marker in self._control.stream_releases:
                if marker in self._command:
                    self._channel.write(f"{marker}:ready\n")
                    await self._control.stream_releases[marker].wait()
                    if not self._closed.is_set():
                        self._channel.write(f"{marker}:finished\n")
                        self._channel.exit(0)
                    return

            if "once-token" in self._command:
                self._channel.write("once-token-ran\n")
            else:
                self._channel.write("command-ran\n")
            self._channel.exit(0)
        except (OSError, asyncssh.Error):
            # Closing a client transport while a server task is waking up is
            # expected in cancellation and blackhole tests.
            return

    def connection_lost(self, exc) -> None:
        del exc
        self._closed.set()
        if self._counted:
            self._counted = False
            self._control.active_sessions -= 1
            self._control.closed_sessions += 1
            self._control.notify()

    def eof_received(self) -> bool:
        # Commands use DEVNULL for stdin but still need to send stdout later.
        return True

    def data_received(self, data, datatype) -> None:
        # The tested runtime uses DEVNULL for these commands' stdin.
        del data, datatype


@pytest.fixture
async def ssh_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    control = _ServerControl()
    server_key = asyncssh.generate_private_key("ssh-ed25519")
    acceptor = await asyncssh.create_server(
        lambda: _LocalSSHServer(control),
        host="127.0.0.1",
        port=0,
        server_host_keys=[server_key],
    )
    control.acceptors.append(acceptor)
    control.server_port = acceptor.get_port()
    control.port = control.server_port

    client_key_path = tmp_path / "client_key"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(client_key_path)
    client_key_path.chmod(0o600)

    runtime = object.__new__(QemuRuntime)
    runtime._ssh_states = {}
    runtime._host_key_cache = {}
    runtime._disk_dir = tmp_path
    runtime._ssh_key_path = client_key_path
    runtime._ssh_user = "root"
    runtime._ssh_timeout = 1
    runtime._get_workspace_vm_ip = MagicMock(return_value="127.0.0.1")

    async def connect_to_local_server(host: str, *args, **kwargs):
        # Leave all production AsyncSSH keyword arguments intact. Only select
        # the ephemeral loopback listener instead of the VM's ordinary port.
        conn = await _REAL_ASYNCSSH_CONNECT(host, *args, port=control.port, **kwargs)
        control.client_connections.append(conn)
        return conn

    monkeypatch.setattr(qemu_runtime.asyncssh, "connect", connect_to_local_server)
    try:
        yield runtime, control
    finally:
        # Unblock fallback cases where an assertion failed during a delayed
        # open, then close every client transport, including ephemeral probes.
        control.release_session_open.set()
        for conn in control.client_connections:
            conn.close()
        await asyncio.gather(
            *(conn.wait_closed() for conn in control.client_connections),
            return_exceptions=True,
        )

        for relay in control.relays:
            await relay.close()
        for conn in list(control.connections):
            conn.close()
        for acceptor in control.acceptors:
            acceptor.close()
        await asyncio.gather(
            *(acceptor.wait_closed() for acceptor in control.acceptors),
            return_exceptions=True,
        )
        for task in list(control.tasks):
            task.cancel()
        if control.tasks:
            await asyncio.gather(*list(control.tasks), return_exceptions=True)
        assert not control.tasks
        assert not control.task_errors


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=timeout)


async def _start_stream(runtime: QemuRuntime, marker: str):
    stream = runtime.exec_command("vm", [marker])
    first_line = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert first_line == f"{marker}:ready\n"
    return stream


async def _finish_stream(control: _ServerControl, stream, marker: str) -> None:
    closed_before = control.closed_sessions
    control.stream_releases[marker].set()
    assert await asyncio.wait_for(stream.__anext__(), timeout=1) == (
        f"{marker}:finished\n"
    )
    # Drain any final empty line and drive the runtime generator through
    # wait_closed and its exit-status check.
    while True:
        try:
            extra_line = await asyncio.wait_for(stream.__anext__(), timeout=1)
        except StopAsyncIteration:
            break
        assert not extra_line, f"unexpected trailing stream data: {extra_line!r}"
    await _wait_until(lambda: control.closed_sessions == closed_before + 1)


async def _cancel_probe_after_open(
    runtime: QemuRuntime, control: _ServerControl, expected_probe: int
) -> None:
    task = asyncio.create_task(runtime.check_reachable("vm"))
    await control.wait_for(
        lambda count=expected_probe: control.probe_sessions_started >= count
    )
    state = runtime._ssh_state("vm")
    probe_connection = state.probe_connection
    assert state.probing
    assert probe_connection is not None
    assert probe_connection is not state.connection

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_until(lambda: probe_connection.is_closed())
    await _wait_until(lambda count=expected_probe: control.closed_sessions >= count)
    assert not state.probing
    assert state.probe_connection is None


async def test_repeated_timed_out_and_cancelled_probes_preserve_two_streams(
    ssh_harness,
) -> None:
    runtime, control = ssh_harness
    alpha = await _start_stream(runtime, "stream-alpha")
    beta = await _start_stream(runtime, "stream-beta")
    await control.wait_for(lambda: control.active_sessions == 2)
    state = runtime._ssh_state("vm")
    shared_connection = state.connection
    assert shared_connection is not None and not shared_connection.is_closed()

    # Timed-out probes use separate transports and close those transports,
    # while both long-lived agent streams retain their shared parent channel.
    for expected in (1, 2):
        probe = asyncio.create_task(runtime.check_reachable("vm"))
        await control.wait_for(
            lambda count=expected: control.probe_sessions_started >= count
        )
        probe_connection = state.probe_connection
        assert state.probing
        assert probe_connection is not None
        assert probe_connection is not shared_connection
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(probe, timeout=0.03)
        await _wait_until(lambda count=expected: control.closed_sessions >= count)
        await _wait_until(lambda conn=probe_connection: conn.is_closed())
        assert not state.probing
        assert state.probe_connection is None
        assert state.connection is shared_connection
        assert not shared_connection.is_closed()
        assert control.active_sessions == 2
        await control.wait_for(lambda: len(control.connections) == 1)

    await _cancel_probe_after_open(runtime, control, 3)
    assert state.connection is shared_connection
    assert not shared_connection.is_closed()
    assert control.active_sessions == 2

    await _finish_stream(control, alpha, "stream-alpha")
    await _finish_stream(control, beta, "stream-beta")
    await control.wait_for(lambda: control.active_sessions == 0)
    assert state.connection is shared_connection
    assert not shared_connection.is_closed()


async def test_channel_resource_shortage_does_not_close_parent_or_streams(
    ssh_harness,
) -> None:
    runtime, control = ssh_harness
    control.max_sessions = 2
    alpha = await _start_stream(runtime, "stream-alpha")
    beta = await _start_stream(runtime, "stream-beta")
    await control.wait_for(lambda: control.active_sessions == 2)
    state = runtime._ssh_state("vm")
    shared_connection = state.connection
    assert shared_connection is not None

    with pytest.raises(asyncssh.ChannelOpenError):
        await runtime.check_reachable("vm")
    assert control.refused_sessions == 1
    await _wait_until(lambda: not state.probing)
    assert state.probe_connection is None
    await control.wait_for(lambda: len(control.connections) == 1)
    assert control.active_sessions == 2
    assert state.connection is shared_connection
    assert not shared_connection.is_closed()

    await _finish_stream(control, alpha, "stream-alpha")
    await _finish_stream(control, beta, "stream-beta")
    await control.wait_for(lambda: control.active_sessions == 0)
    assert not shared_connection.is_closed()


async def test_cancelled_probe_at_delayed_channel_open_closes_its_transport(
    ssh_harness,
) -> None:
    runtime, control = ssh_harness
    control.delay_session_open = True

    probe = asyncio.create_task(runtime.check_reachable("vm"))
    await control.wait_for(lambda: control.pending_session_opens == 1)
    state = runtime._ssh_state("vm")
    delayed_connection = state.probe_connection
    assert state.probing
    assert delayed_connection is not None

    # A concurrent check observes the active probe and does not open another
    # client transport while the server is withholding its channel-open reply.
    assert await runtime.check_reachable("vm") is False
    assert control.connection_count == 1
    assert control.pending_session_opens == 1

    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    await _wait_until(lambda: delayed_connection.is_closed())
    await _wait_until(lambda: control.pending_session_opens == 0)
    await _wait_until(lambda: not control.connections)
    assert not state.probing
    assert state.probe_connection is None
    assert control.opened_sessions == 0

    # A new check can immediately open its own connection and channel; the
    # aborted open above did not linger or consume a server session slot.
    control.delay_session_open = False
    await _cancel_probe_after_open(runtime, control, 1)
    await _wait_until(lambda: control.active_sessions == 0)
    await _wait_until(lambda: not control.connections)
    assert control.opened_sessions == 1
    assert control.closed_sessions == 1
    assert control.connection_count == 2


async def test_parallel_get_ssh_reconnects_after_real_disconnect_without_replay(
    ssh_harness,
) -> None:
    runtime, control = ssh_harness
    exit_code, output = await runtime.exec_command_wait("vm", ["once-token"])
    assert exit_code == 0
    assert "once-token-ran" in output
    assert sum("once-token" in command for command in control.commands) == 1

    old_connection = runtime._ssh_state("vm").connection
    assert old_connection is not None
    server_connection = next(iter(control.connections))
    server_connection.abort()
    await _wait_until(lambda: old_connection.is_closed())
    await _wait_until(lambda: not control.connections)

    connections = await asyncio.gather(*(runtime._get_ssh("vm") for _ in range(8)))
    replacement = connections[0]
    assert replacement is not old_connection
    assert all(conn is replacement for conn in connections)
    assert not replacement.is_closed()
    await _wait_until(lambda: control.connection_count == 2)
    assert sum("once-token" in command for command in control.commands) == 1


async def test_reconnect_rejects_a_real_changed_host_key(ssh_harness) -> None:
    runtime, control = ssh_harness
    exit_code, output = await runtime.exec_command_wait("vm", ["once-token"])
    assert exit_code == 0
    assert "once-token-ran" in output
    state = runtime._ssh_state("vm")
    old_connection = state.connection
    assert old_connection is not None

    next(iter(control.connections)).abort()
    await _wait_until(lambda: old_connection.is_closed())
    await _wait_until(lambda: not control.connections)

    changed_key_acceptor = await asyncssh.create_server(
        lambda: _LocalSSHServer(control),
        host="127.0.0.1",
        port=0,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
    )
    control.acceptors.append(changed_key_acceptor)
    control.port = changed_key_acceptor.get_port()

    with pytest.raises(RuntimeError, match="SSH host key mismatch"):
        await runtime._get_ssh("vm")
    await _wait_until(lambda: control.connection_count == 2)
    await _wait_until(lambda: not control.connections)
    assert state.connection is None
    assert sum("once-token" in command for command in control.commands) == 1


async def test_keepalive_survives_exec_channel_refusals(ssh_harness) -> None:
    runtime, control = ssh_harness
    control.max_sessions = 0
    shared_connection = await runtime._get_ssh("vm")
    shared_connection.set_keepalive(0.02, 1)

    # Check one refusal on the shared transport itself, then exercise the
    # runtime health probe which owns and closes a separate refused transport.
    with pytest.raises(asyncssh.ChannelOpenError):
        await shared_connection.create_process("echo ok")
    with pytest.raises(asyncssh.ChannelOpenError):
        await runtime.check_reachable("vm")
    await control.wait_for(lambda: control.refused_sessions == 2)
    await _wait_until(lambda: len(control.connections) == 1)

    await asyncio.wait_for(asyncio.sleep(0.1), timeout=0.2)
    state = runtime._ssh_state("vm")
    assert state.connection is shared_connection
    assert not shared_connection.is_closed()
    assert await runtime._get_ssh("vm") is shared_connection


async def test_keepalive_detects_a_real_blackholed_transport_without_replay(
    ssh_harness,
) -> None:
    runtime, control = ssh_harness
    relay = _TCPRelay(control.server_port)
    control.port = await relay.start()
    control.relays.append(relay)

    exit_code, output = await runtime.exec_command_wait("vm", ["once-token"])
    assert exit_code == 0
    assert "once-token-ran" in output
    old_connection = runtime._ssh_state("vm").connection
    assert old_connection is not None
    old_connection.set_keepalive(0.03, 1)

    # TCP remains established while the relay silently drops both directions,
    # so only SSH's protocol keepalive detects the dead path.
    relay.blackhole = True
    await _wait_until(lambda: old_connection.is_closed(), timeout=2)
    await _wait_until(lambda: not control.connections, timeout=2)
    relay.blackhole = False

    replacement = await runtime._get_ssh("vm")
    assert replacement is not old_connection
    exit_code, output = await runtime.exec_command_wait("vm", ["after-blackhole"])
    assert exit_code == 0
    assert "command-ran" in output
    await _wait_until(lambda: control.connection_count == 2)
    assert sum("once-token" in command for command in control.commands) == 1
