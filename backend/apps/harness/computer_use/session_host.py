"""One Computer Use REPL per chat, kept across the chat's runs.

A host owns the chat's Computer Use MCP connection and its desktop lease in
one actor task, which enters and exits the MCP SDK's anyio scopes. Runs attach
one at a time. At run end the turn is stopped (the server keeps its JavaScript
state) and the display claim is released exactly like a final release would,
while the process and the lease reservation stay for the next run. The idle
timeout, a lease or connection failure, a runner restart, a configuration
change, chat deletion and shutdown close the host. The registry is
process-local: agents run only in the execution process.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from uuid import uuid4

import anyio
import structlog
from asgiref.sync import ThreadSensitiveContext

from ..agent_desktop import AgentDesktop
from ..mcp_client.connection import McpServerConnection

log = structlog.get_logger(__name__)

#: Default for the ``COMPUTER_USE_SESSION_IDLE_SECONDS`` setting.
DEFAULT_IDLE_SECONDS = 15 * 60
#: Bound of each reuse probe (runner lease status, MCP ping).
HEALTH_TIMEOUT_SECONDS = 5.0

OpenConnection = Callable[[McpServerConnection], Awaitable[None]]


def idle_timeout_seconds() -> float:
    """Seconds a host stays open after its last run detached."""
    from django.conf import settings

    return float(
        getattr(settings, "COMPUTER_USE_SESSION_IDLE_SECONDS", DEFAULT_IDLE_SECONDS)
    )


def connection_fingerprint(connection: McpServerConnection, **extra: Any) -> str:
    """Digest of the started server's configuration; keeps no plaintext secrets."""
    payload = {
        "plugin_id": connection.plugin_id,
        "server_id": connection.server_id,
        "transport": connection.transport,
        "command": list(connection.command),
        "cwd": connection.cwd,
        "env": dict(connection.env),
        "url": connection.url,
        "headers": dict(connection.headers),
        "oauth_credential_id": connection.oauth_credential_id,
        "tool_policy": connection.tool_policy,
        "startup_timeout_seconds": connection.startup_timeout_seconds,
        "request_timeout_seconds": connection.request_timeout_seconds,
        "desktop_activation": connection.desktop_activation,
        **extra,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _join(task: asyncio.Future) -> bool:
    """Wait for *task* without raising its outcome; report caller cancellation.

    The shield only nests inside the caller's scopes (never exits them), so it
    is safe in a task that owns SDK scopes, like ``cleanup_rpc``.
    """
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
    return cancelled


class HostAttachment:
    """One run's use of a kept host; detached exactly once at run end."""

    def __init__(self, host: ComputerUseSessionHost, task: asyncio.Task | None) -> None:
        self.host = host
        #: Cancelled when the desktop lease fails while this run is attached.
        self.task = task
        self.detached = False

    async def detach(self) -> None:
        """Stop the run's turn and display claim; keep the host for the next run."""
        await self.host.detach(self)


class ComputerUseSessionHost:
    """Own one chat's Computer Use connection and desktop lease in an actor task."""

    def __init__(
        self,
        *,
        session_id: str,
        workspace_id: str,
        fingerprint: str,
        connection: McpServerConnection,
        accessor: Any,
        open_connection: OpenConnection,
        idle_seconds: float,
        on_closed: Callable[[ComputerUseSessionHost], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.workspace_id = str(workspace_id)
        self.fingerprint = fingerprint
        self.connection = connection
        self.loop = asyncio.get_running_loop()
        self.desktop = AgentDesktop(
            accessor, str(uuid4()), on_failure=self._on_failure, kind="computeruse"
        )
        connection.agent_desktop = self.desktop
        self.close_reason = ""
        self._accessor = accessor
        self._open_connection = open_connection
        self._idle_seconds = idle_seconds
        self._on_closed = on_closed
        self._attachment: HostAttachment | None = None
        self._ready: asyncio.Future[None] = self.loop.create_future()
        self._stopped = asyncio.Event()
        self._idle_timer: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._log = log.bind(session_id=session_id, workspace_id=self.workspace_id)

    @property
    def closing(self) -> bool:
        """Whether the host stopped accepting runs."""
        return self._stopped.is_set()

    @property
    def closed(self) -> bool:
        """Whether the actor finished its teardown."""
        return self._task is not None and self._task.done()

    @property
    def attached(self) -> bool:
        """Whether a run currently uses the host."""
        return self._attachment is not None

    def start(self, task: asyncio.Task | None) -> HostAttachment:
        """Start the actor with the creating run already attached."""
        attachment = HostAttachment(self, task)
        self._attachment = attachment
        # Outlives the creating run: detach from its asgiref executor (see
        # HarnessService._spawn_background) and own an ORM thread instead.
        self._task = self.loop.create_task(
            self._own_in_context(),
            name=f"computer-use-host:{self.session_id}",
            context=contextvars.Context(),
        )
        return attachment

    async def wait_ready(self) -> None:
        """Wait until the server is started; raise its startup failure."""
        await asyncio.shield(self._ready)
        if self.closing:
            raise RuntimeError("Computer Use host closed during startup")

    def _started(self) -> bool:
        return (
            self._ready.done()
            and not self._ready.cancelled()
            and self._ready.exception() is None
        )

    async def _own_in_context(self) -> None:
        async with ThreadSensitiveContext():
            await self._own()

    async def _own(self) -> None:
        try:
            try:
                await self.desktop.prepare()
                self.connection.desktop_lease = self.desktop.lease
                await self._open_connection(self.connection)
            except BaseException as exc:
                if not self._ready.done():
                    if isinstance(exc, asyncio.CancelledError):
                        self._ready.cancel()
                    else:
                        self._ready.set_exception(exc)
                        # The creating run may already have stopped waiting.
                        self._ready.exception()
                raise
            self.desktop.before_stop.append(self.connection.stop_desktop_work)
            self._ready.set_result(None)
            self._log.info("computer_use_host_started")
            await self._stopped.wait()
        except asyncio.CancelledError:
            self.close_reason = self.close_reason or "cancelled"
            raise
        except Exception:
            # Reported to the creating run through the ready future.
            self.close_reason = self.close_reason or "startup_failed"
        finally:
            self._stopped.set()
            self._cancel_idle_timer()
            # SDK scopes were entered in this task and are exited here, LIFO.
            # Never wrap this in a new cancel scope: anyio requires strict nesting.
            try:
                await self.connection.aclose()
            finally:
                try:
                    await self.desktop.aclose()
                except Exception:
                    self._log.warning("computer_use_host_release_failed")
                self._log.info("computer_use_host_closed", reason=self.close_reason)
                if self._on_closed is not None:
                    self._on_closed(self)

    def try_attach(self, task: asyncio.Task | None) -> HostAttachment | None:
        """Reserve the started host for one run, or return None when unavailable."""
        if self.closing or self._attachment is not None or not self._started():
            return None
        self._cancel_idle_timer()
        self._attachment = HostAttachment(self, task)
        return self._attachment

    def release_unbound(self, attachment: HostAttachment) -> None:
        """Drop a reservation that never served a run; the idle clock restarts."""
        attachment.detached = True
        if self._attachment is attachment:
            self._attachment = None
            if not self.closing and self._started():
                self._arm_idle_timer()

    def bind(
        self,
        attachment: HostAttachment,
        run_context: dict[str, Any],
        recording_owner: str,
    ) -> None:
        """Point the kept connection and desktop at the attached run."""
        assert self._attachment is attachment
        self.connection.begin_run(run_context)
        self.desktop.begin_run(recording_owner)

    async def healthy(self) -> bool:
        """Probe the lease at the runner (live, same epoch) and the MCP server."""
        connection, lease = self.connection, self.desktop.lease
        if self.closing or not self._started() or lease is None:
            return False
        if lease.closed or not lease.healthy or not connection.healthy:
            return False
        try:
            status = await asyncio.wait_for(
                self._accessor.desktop_action("lease_status", lease._args()),
                HEALTH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        if (
            not isinstance(status, dict)
            or not status.get("ok")
            or status.get("epoch") != lease.epoch
            or not status.get("lease_live")
        ):
            return False
        return await connection.check_alive(HEALTH_TIMEOUT_SECONDS)

    async def _end_run(self) -> bool:
        try:
            # "Stop" fences the run's turn; the server keeps its state.
            await self.connection.finish()
            if not self.connection.healthy:
                return False
            await self.desktop.detach()
        except Exception:
            self._log.warning("computer_use_host_detach_failed", exc_info=True)
            return False
        return True

    async def detach(self, attachment: HostAttachment) -> None:
        """End the run's use; close the host instead when that is unconfirmed."""
        if attachment.detached:
            return
        attachment.detached = True
        kept = False
        cancelled = False
        try:
            if not self.closing and self._started():
                # One task finishes the RPCs despite cancellation of the run.
                work = asyncio.ensure_future(self._end_run())
                cancelled = await _join(work)
                kept = not work.cancelled() and bool(work.result())
        finally:
            if self._attachment is attachment:
                self._attachment = None
            if kept and not self.closing:
                self._arm_idle_timer()
            else:
                cancelled = await self._close_and_join("detach_failed") or cancelled
        if cancelled:
            raise asyncio.CancelledError()

    def _on_failure(self) -> None:
        """Lease renewal or activation failed: interrupt the run, close the host."""
        attachment = self._attachment
        if attachment is not None and attachment.task is not None:
            attachment.task.cancel()
        self.request_close("desktop_lease_failed")

    def request_close(self, reason: str) -> None:
        """Ask the actor to close; safe to call repeatedly from the loop thread."""
        if not self._stopped.is_set():
            self.close_reason = reason
            self._log.info("computer_use_host_closing", reason=reason)
        self._cancel_idle_timer()
        self._stopped.set()

    def request_close_threadsafe(self, reason: str) -> None:
        """Ask the actor to close from any thread."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.request_close(reason)
        elif not self.loop.is_closed():
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.request_close, reason)

    async def _close_and_join(self, reason: str) -> bool:
        self.request_close(reason)
        task = self._task
        if task is None or task is asyncio.current_task():
            return False
        return await _join(task)

    async def aclose(self, reason: str = "closed") -> None:
        """Close the connection and release the lease, waiting for the actor."""
        if await self._close_and_join(reason):
            raise asyncio.CancelledError()

    def _arm_idle_timer(self) -> None:
        self._cancel_idle_timer()
        self._idle_timer = self.loop.call_later(self._idle_seconds, self._expire)

    def _cancel_idle_timer(self) -> None:
        timer, self._idle_timer = self._idle_timer, None
        if timer is not None:
            timer.cancel()

    def _expire(self) -> None:
        self._idle_timer = None
        if self._attachment is None:
            self.request_close("idle")


class ComputerUseSessionHosts:
    """Process-local registry of kept hosts, one per chat session."""

    def __init__(self) -> None:
        self._hosts: dict[str, ComputerUseSessionHost] = {}
        self._attaching: set[str] = set()

    def get(self, session_id: str) -> ComputerUseSessionHost | None:
        """The registered host of *session_id* on the running loop, if any."""
        host = self._hosts.get(str(session_id))
        if host is None:
            return None
        if host.closed or host.loop is not asyncio.get_running_loop():
            self._forget(host)
            return None
        return host

    async def attach(
        self,
        *,
        session_id: str,
        workspace_id: str,
        connection: McpServerConnection,
        fingerprint: str,
        accessor: Any,
        open_connection: OpenConnection,
        run_context: dict[str, Any],
        recording_owner: str,
        task: asyncio.Task | None,
    ) -> HostAttachment | None:
        """Attach a run to its chat's host, replacing a host that cannot serve it.

        Returns None while another run of the chat is attached or attaching;
        that run then uses its own per-run connection. *connection* is opened
        only when a new host starts.
        """
        session_id = str(session_id)
        if session_id in self._attaching:
            return None
        self._attaching.add(session_id)
        try:
            host = self.get(session_id)
            if host is not None:
                if host.attached:
                    return None
                reason = "configuration_changed"
                if host.workspace_id == str(workspace_id) and (
                    host.fingerprint == fingerprint
                ):
                    attachment = host.try_attach(task)
                    if attachment is not None:
                        try:
                            healthy = await host.healthy()
                        except BaseException:
                            # The run was cancelled while probing.
                            host.release_unbound(attachment)
                            raise
                        if healthy:
                            host.bind(attachment, run_context, recording_owner)
                            log.info(
                                "computer_use_host_reused",
                                session_id=session_id,
                                workspace_id=host.workspace_id,
                            )
                            return attachment
                        host.release_unbound(attachment)
                    reason = "unhealthy"
                await host.aclose(reason)
            host = ComputerUseSessionHost(
                session_id=session_id,
                workspace_id=str(workspace_id),
                fingerprint=fingerprint,
                connection=connection,
                accessor=accessor,
                open_connection=open_connection,
                idle_seconds=idle_timeout_seconds(),
                on_closed=self._forget,
            )
            self._hosts[session_id] = host
            attachment = host.start(task)
            try:
                await host.wait_ready()
            except BaseException as exc:
                host.release_unbound(attachment)
                cancelled = isinstance(exc, asyncio.CancelledError)
                await host.aclose(
                    "startup_cancelled" if cancelled else "startup_failed"
                )
                raise
            host.bind(attachment, run_context, recording_owner)
            return attachment
        finally:
            self._attaching.discard(session_id)

    def _forget(self, host: ComputerUseSessionHost) -> None:
        if self._hosts.get(host.session_id) is host:
            del self._hosts[host.session_id]

    async def close_idle(self, session_id: str, *, reason: str) -> None:
        """Close the chat's host unless a run is attached."""
        host = self.get(str(session_id))
        if host is not None and not host.attached:
            await host.aclose(reason)

    async def close_sessions(self, session_ids: Iterable[Any], *, reason: str) -> None:
        """Close the hosts of *session_ids* (e.g. deleted chats)."""
        hosts = [self.get(str(session_id)) for session_id in session_ids]
        await self._close([host for host in hosts if host is not None], reason)

    async def close_all(self, *, reason: str) -> None:
        """Close every host of the running loop (best effort, e.g. at shutdown)."""
        loop = asyncio.get_running_loop()
        await self._close(
            [host for host in list(self._hosts.values()) if host.loop is loop], reason
        )

    def close_workspaces(
        self, matches_workspace: Callable[[str], bool], *, reason: str
    ) -> None:
        """Close hosts of matching workspaces; callable from any thread."""
        for host in list(self._hosts.values()):
            try:
                matched = matches_workspace(host.workspace_id)
            except Exception:
                continue
            if matched:
                host.request_close_threadsafe(reason)

    @staticmethod
    async def _close(hosts: list[ComputerUseSessionHost], reason: str) -> None:
        if hosts:
            await asyncio.gather(
                *(host.aclose(reason) for host in hosts), return_exceptions=True
            )


#: The execution process's hosts.
session_hosts = ComputerUseSessionHosts()
