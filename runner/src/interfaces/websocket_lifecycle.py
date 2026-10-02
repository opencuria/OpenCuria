"""Confirmed Socket.IO sessions with a single, cancellation-aware retry owner."""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import socketio
import structlog

from ..config import RunnerSettings

PROTOCOL_VERSION = 1
logger = structlog.get_logger(__name__)


class FatalConnectionError(RuntimeError):
    """An explicit authentication, protocol, configuration, or cleanup failure."""


class SessionLostError(RuntimeError):
    """A transient transport or backend confirmation failure."""


@dataclass
class RunnerSession:
    """Transport and signals belonging to exactly one connection attempt."""

    client: socketio.AsyncClient
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    rejection: dict | None = None
    heartbeat_confirmed: bool = False

    def require_transport(self) -> None:
        """Reject half-connected clients and sessions already disconnected."""
        if (
            self.disconnected.is_set()
            or not self.client.connected
            or self.client.eio.state != "connected"
        ):
            raise SessionLostError("transport_lost")

    async def confirm(self, event: str, data: dict, timeout: float) -> dict:
        """Require a semantic ACK from this session, never an empty legacy ACK."""
        self.require_transport()
        reply = await self.client.call(event, data, timeout=timeout)
        self.require_transport()
        if not isinstance(reply, dict):
            raise FatalConnectionError("invalid_protocol_ack")
        if reply.get("ok") is not True:
            code = str(reply.get("code", "registration_rejected"))
            if reply.get("retryable") is False:
                raise FatalConnectionError(code)
            raise SessionLostError(code)
        if (
            reply.get("protocol_version") != PROTOCOL_VERSION
            or reply.get("sid") != self.client.get_sid()
        ):
            raise FatalConnectionError("invalid_protocol_ack")
        return reply


async def cancel_tasks(tasks: list[asyncio.Task], timeout: float) -> None:
    """Cancel and drain owned tasks without silently abandoning survivors."""
    tasks = [task for task in tasks if task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        if not task.cancelled():
            task.exception()  # Retrieve errors even when a session was interrupted.
    if pending:
        raise FatalConnectionError("session_cleanup_timeout")


class ConnectionSupervisor:
    """Own connect, confirmation, liveness, retry, and final session teardown."""

    def __init__(
        self,
        settings: RunnerSettings,
        create_client: Callable[[], socketio.AsyncClient],
        prepare: Callable[[RunnerSession], Awaitable[list[dict]]],
        activate: Callable[[RunnerSession, list[dict]], Awaitable[None]],
        cleanup: Callable[[RunnerSession], Awaitable[None]],
        supported_runtimes: list[str],
    ) -> None:
        self.settings = settings
        self.create_client = create_client
        self.prepare = prepare
        self.activate = activate
        self.cleanup = cleanup
        self.supported_runtimes = supported_runtimes
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _until_disconnect(
        self, session: RunnerSession, operation: Awaitable, timeout: float | None = None
    ):
        """Race work against transport loss and drain both tasks on every exit."""
        work = asyncio.ensure_future(operation)
        disconnected = asyncio.create_task(session.disconnected.wait())
        try:
            done, _ = await asyncio.wait(
                [work, disconnected],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done:
                raise SessionLostError("transport_lost")
            if work not in done:
                raise SessionLostError("operation_timeout")
            return await work
        finally:
            await cancel_tasks([work, disconnected], self.settings.cleanup_timeout)

    async def _heartbeat(self, session: RunnerSession) -> None:
        while True:
            await session.confirm(
                "runner:heartbeat", {}, self.settings.connection_timeout
            )
            session.heartbeat_confirmed = True
            logger.debug("heartbeat_confirmed")
            await asyncio.sleep(self.settings.heartbeat_interval)

    async def _run_session(self, session: RunnerSession) -> None:
        client = session.client

        @client.event
        async def connect() -> None:
            # Do not await runtime work in Socket.IO's handshake callback.
            logger.info("websocket_connected")

        @client.event
        async def disconnect(reason: str | None = None) -> None:
            session.disconnected.set()
            logger.warning("websocket_disconnected")

        @client.event
        async def connect_error(data: object) -> None:
            # Only explicit structured codes classify permanent remote failures.
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                session.rejection = data["data"]

        try:
            await self._until_disconnect(
                session,
                client.connect(
                    self.settings.backend_url,
                    headers={"Authorization": f"Bearer {self.settings.api_token}"},
                    auth={
                        "token": self.settings.api_token,
                        "protocol_version": PROTOCOL_VERSION,
                    },
                    transports=["websocket"],
                    socketio_path=self.settings.socketio_path,
                    wait_timeout=self.settings.connection_timeout,
                ),
                self.settings.connection_timeout,
            )
        except (socketio.exceptions.ConnectionError, SessionLostError):
            if session.rejection and session.rejection.get("retryable") is False:
                raise FatalConnectionError(
                    str(session.rejection.get("code", "authentication_rejected"))
                ) from None
            raise
        session.require_transport()
        snapshot = await self._until_disconnect(
            session, self.prepare(session), self.settings.runtime_setup_timeout
        )
        await self._until_disconnect(
            session,
            session.confirm(
                "runner:register",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "supported_runtimes": self.supported_runtimes,
                    "status": "ready",
                },
                self.settings.connection_timeout,
            ),
            self.settings.connection_timeout,
        )
        await self._until_disconnect(
            session, self.activate(session, snapshot), self.settings.connection_timeout
        )
        logger.info("runner_registered", sid=client.get_sid())
        await self._until_disconnect(session, self._heartbeat(session))

    async def _close_transport(self, session: RunnerSession) -> None:
        """Close even a half-connected Engine.IO client, not just Socket.IO flags."""
        client = session.client
        # abort avoids waiting on a receive task executing an application handler.
        await client.eio.disconnect(abort=True)
        tasks = [
            task
            for task in (client.eio.read_loop_task, client.eio.write_loop_task)
            if task is not None
        ]
        await cancel_tasks(tasks, self.settings.cleanup_timeout)

    async def run(self) -> None:
        """Retry transient failures indefinitely; fail closed for permanent ones."""
        self._task = asyncio.current_task()
        delay = self.settings.reconnect_delay
        try:
            while not self._stop.is_set():
                session = RunnerSession(self.create_client())
                try:
                    logger.info("websocket_connecting")
                    await self._run_session(session)
                except FatalConnectionError:
                    raise
                except (
                    SessionLostError,
                    socketio.exceptions.SocketIOError,
                    OSError,
                    asyncio.TimeoutError,
                ) as exc:
                    # Exception strings may contain credentials from HTTP errors.
                    logger.warning("runner_session_lost", error_type=type(exc).__name__)
                finally:
                    session.disconnected.set()
                    try:
                        await asyncio.wait_for(
                            self._close_transport(session),
                            self.settings.cleanup_timeout * 2,
                        )
                    finally:
                        await self.cleanup(session)
                if self._stop.is_set():
                    break
                if session.heartbeat_confirmed:
                    delay = self.settings.reconnect_delay
                seconds = min(
                    self.settings.reconnect_delay_max, delay * random.uniform(0.8, 1.2)
                )
                logger.info("runner_reconnect_scheduled", delay_seconds=seconds)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), seconds)
                delay = min(self.settings.reconnect_delay_max, delay * 2)
        finally:
            self._task = None

    async def stop(self) -> None:
        """Interrupt connect, runtime preparation, heartbeat, or retry promptly."""
        self._stop.set()
        if self._task and self._task is not asyncio.current_task():
            task = self._task
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
