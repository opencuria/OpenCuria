"""Backends of published apps: processes in the workspace an app came from.

An app addresses a backend as ``http://<process>.opencuria.local``. This
service authorizes such a request for the app's owner, makes sure the
workspace runs (resuming a stopped one), starts the named persistent process
when it is not running and finds the port it listens on. It also issues the
app session tokens the OpenCuria mobile app attaches to gateway requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from asgiref.sync import sync_to_async
from django.core import signing
from django.utils import timezone

from apps.harness.access.base import WorkspaceByteStream
from apps.harness.access.runner_accessor import create_harness_accessor
from apps.runners.enums import ProcessKind, ProcessStatus, WorkspaceOperation
from apps.runners.enums import WorkspaceStatus as Status
from apps.runners.services import RunnerService, get_runner_service
from common.exceptions import ConflictError, NotFoundError, ServiceError

from .gateway_protocol import BackendAddress, gateway_path
from .listening_ports import LISTENING_COMMAND, ListeningSnapshot, parse_listening
from .repositories import AppRepository

log = structlog.get_logger(__name__)

#: App session tokens: signed, scoped to one app and its owner.
SESSION_SALT = "opencuria.mobile_apps.gateway_session"
SESSION_TTL = timedelta(hours=12)

#: How long one request waits for a stopped workspace to resume. Requests
#: that wait longer get 503 with Retry-After; the resume continues.
WORKSPACE_WAIT_SECONDS = 40.0
#: How long one request waits for a started process to listen.
PORT_WAIT_SECONDS = 25.0
#: How long one request waits for another start of the process to confirm.
PROCESS_START_WAIT_SECONDS = 10.0
#: Detected ports are reused this long (a failed connection forgets them).
PORT_CACHE_SECONDS = 15.0
#: Gateway traffic keeps the workspace from stopping for inactivity.
ACTIVITY_TOUCH_SECONDS = 60.0
#: Upstream connections per workspace. Each is a runner TCP stream, which
#: agents' MCP servers share, so the gateway keeps to a bounded share.
HTTP_CONNECTIONS_PER_WORKSPACE = 6
WEBSOCKETS_PER_WORKSPACE = 4
#: How long an HTTP request waits for a free upstream connection.
CONNECTION_WAIT_SECONDS = 30.0
STREAM_OPEN_TIMEOUT = 20.0
PROBE_TIMEOUT = 10.0


class BackendError(ServiceError):
    """A backend request that cannot be forwarded, with its HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message=message, code=code)
        self.status = status
        self.retry_after = retry_after


def _unavailable(message: str, retry_after: int = 5) -> BackendError:
    return BackendError(
        message, status=503, code="backend_unavailable", retry_after=retry_after
    )


class _ProcessExitedError(Exception):
    """The process ended while the gateway waited for it to listen."""

    def __init__(self, process: Any) -> None:
        super().__init__(process.name)
        self.process = process


def _exited(process: Any) -> BackendError:
    code = process.exit_code
    return BackendError(
        f"Process '{process.name}' exited"
        + (f" with code {code}" if code is not None else "")
        + (f". Logs: {process.log_path}" if process.log_path else ".")
        + " It restarts with the next request.",
        status=502,
        code="backend_process_exited",
    )


@dataclass(frozen=True)
class GatewaySession:
    """What the OpenCuria app needs to route an app's backend requests."""

    token: str
    #: Gateway path of the app on the server (append ``/<label>/<port>/...``).
    path: str
    expires_at: datetime


@dataclass(frozen=True)
class SessionClaims:
    """The app and owner an app session token was issued for."""

    app_id: uuid.UUID
    organization_id: uuid.UUID
    owner_id: int


@dataclass(frozen=True)
class BackendTarget:
    """A running workspace process and the port requests go to."""

    workspace_id: uuid.UUID
    process_id: uuid.UUID
    process_name: str
    pid: int
    port: int
    address: BackendAddress


AccessorFactory = Callable[[str], Awaitable[Any]]


async def _runner_accessor(workspace_id: str) -> Any:
    return await create_harness_accessor(get_runner_service(), workspace_id)


class _BudgetedStream(WorkspaceByteStream):
    """A workspace stream that returns its connection budget when closed."""

    def __init__(self, stream: WorkspaceByteStream, release: Callable[[], None]):
        super().__init__(stream.workspace_id, stream.connection_id)
        self._stream = stream
        self._release: Callable[[], None] | None = release

    async def receive(self) -> bytes:
        return await self._stream.receive()

    async def send(self, data: bytes) -> None:
        await self._stream.send(data)

    async def send_eof(self) -> None:
        await self._stream.send_eof()

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            release, self._release = self._release, None
            if release is not None:
                release()

    async def wait_closed(self) -> int | None:
        return await self._stream.wait_closed()


class AppBackendService:
    """Routes app backend requests to processes of the app's workspace.

    One instance serves the gateway for the process lifetime: it keeps the
    per-workspace locks, connection budgets and short-lived port cache.
    """

    def __init__(
        self,
        repository: type[AppRepository] = AppRepository,
        runners: Callable[[], RunnerService] = get_runner_service,
        accessor_factory: AccessorFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.apps = repository
        self._runners = runners
        self._accessor_factory = accessor_factory or _runner_accessor
        self._clock = clock
        self._locks: dict[tuple[str, ...], asyncio.Lock] = {}
        self._budgets: dict[tuple[uuid.UUID, bool], asyncio.Semaphore] = {}
        self._ports: dict[tuple[uuid.UUID, int, int | None], tuple[int, float]] = {}
        self._touched: dict[uuid.UUID, float] = {}

    # -- App sessions -----------------------------------------------------

    def issue_session(
        self, app_id: uuid.UUID, *, organization_id: uuid.UUID, owner_id: int
    ) -> GatewaySession:
        """Issue the token that lets the user's device reach the app's backends.

        Raises:
            NotFoundError: For unknown or foreign apps.
            ConflictError: When the app's workspace is gone or not the user's.
        """
        app = self.apps.get_owned(app_id, organization_id, owner_id)
        if app is None:
            raise NotFoundError("App", str(app_id))
        if app.workspace_id is None or not self._runners().is_active_workspace_owner(
            owner_id, app.workspace_id, organization_id=organization_id
        ):
            raise ConflictError(
                "The workspace this app was published from no longer exists."
            )
        token = signing.dumps(
            {"a": str(app.id), "o": str(organization_id), "u": owner_id},
            salt=SESSION_SALT,
            compress=False,
        )
        return GatewaySession(
            token=token,
            path=gateway_path(app.id),
            expires_at=timezone.now() + SESSION_TTL,
        )

    @staticmethod
    def verify_session(token: str, app_id: str) -> SessionClaims:
        """Return the claims of a valid token issued for *app_id*.

        Raises:
            BackendError: 401 for missing, foreign, expired or forged tokens.
        """
        denied = BackendError(
            "Open the app from OpenCuria to reach its backend.",
            status=401,
            code="backend_session_invalid",
        )
        if not token:
            raise denied
        try:
            claims = signing.loads(
                token, salt=SESSION_SALT, max_age=SESSION_TTL.total_seconds()
            )
            parsed = SessionClaims(
                app_id=uuid.UUID(str(claims["a"])),
                organization_id=uuid.UUID(str(claims["o"])),
                owner_id=int(claims["u"]),
            )
        except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
            raise denied from exc
        if str(parsed.app_id) != app_id.lower():
            raise denied
        return parsed

    # -- Resolving a backend ----------------------------------------------

    async def resolve(
        self, claims: SessionClaims, address: BackendAddress
    ) -> BackendTarget:
        """Find (and if needed start) the process *address* names.

        Raises:
            BackendError: With the status the gateway answers.
        """
        workspace_id = await self._authorized_workspace(claims)
        await self._ensure_workspace_running(workspace_id)
        process = await self._find_process(workspace_id, address.label)
        process = await self._ensure_process_running(workspace_id, process)
        try:
            port = await self._port(workspace_id, process, address.explicit_port)
        except _ProcessExitedError as gone:
            # It died after the last heartbeat or while starting: once more.
            process = await self._ensure_process_running(workspace_id, gone.process)
            try:
                port = await self._port(workspace_id, process, address.explicit_port)
            except _ProcessExitedError as again:
                raise _exited(again.process) from None
        await self._touch(workspace_id)
        return BackendTarget(
            workspace_id=workspace_id,
            process_id=process.id,
            process_name=process.name,
            pid=process.pid,
            port=port,
            address=address,
        )

    def forget(self, target: BackendTarget) -> None:
        """Drop the cached port after a failed connection (re-detect next time)."""
        self._ports.pop(
            (target.workspace_id, target.pid, target.address.explicit_port), None
        )

    @contextlib.asynccontextmanager
    async def keep_active(self, workspace_id: uuid.UUID) -> AsyncIterator[None]:
        """Count a long request or socket as workspace activity while it lasts."""

        async def touch() -> None:
            while True:
                await asyncio.sleep(ACTIVITY_TOUCH_SECONDS)
                await self._touch(workspace_id)

        task = asyncio.create_task(touch())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def open_stream(
        self, target: BackendTarget, *, websocket: bool = False
    ) -> WorkspaceByteStream:
        """Open a TCP connection to the target port inside the workspace.

        Connections count against the workspace's gateway budget until the
        returned stream is closed.

        Raises:
            BackendError: 503 when the workspace has no free connection.
        """
        budget = self._budgets.setdefault(
            (target.workspace_id, websocket),
            asyncio.Semaphore(
                WEBSOCKETS_PER_WORKSPACE
                if websocket
                else HTTP_CONNECTIONS_PER_WORKSPACE
            ),
        )
        busy = _unavailable(
            "Too many open connections to the workspace; try again shortly.",
            retry_after=2,
        )
        if websocket and budget.locked():
            raise busy  # Sockets are long-lived: refuse instead of queueing.
        try:
            await asyncio.wait_for(budget.acquire(), CONNECTION_WAIT_SECONDS)
        except TimeoutError as exc:
            raise busy from exc
        try:
            accessor = await self._accessor_factory(str(target.workspace_id))
            stream = await accessor.open_tcp(
                "localhost", target.port, timeout=STREAM_OPEN_TIMEOUT
            )
        except BaseException:
            budget.release()
            raise
        return _BudgetedStream(stream, budget.release)

    # -- Steps ------------------------------------------------------------

    def _lock(self, *key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def _authorized_workspace(self, claims: SessionClaims) -> uuid.UUID:
        app = await sync_to_async(self.apps.get_owned)(
            claims.app_id, claims.organization_id, claims.owner_id
        )
        gone = BackendError(
            "This app is no longer available on the server.",
            status=404,
            code="backend_app_not_found",
        )
        if app is None:
            raise gone
        if app.workspace_id is None or not await sync_to_async(
            self._runners().is_active_workspace_owner
        )(claims.owner_id, app.workspace_id, organization_id=claims.organization_id):
            raise BackendError(
                "The workspace this app was published from no longer exists.",
                status=404,
                code="backend_workspace_not_found",
            )
        return app.workspace_id

    async def _ensure_workspace_running(self, workspace_id: uuid.UUID) -> None:
        """Wait for a running workspace, resuming it when it is stopped."""
        runners = self._runners()
        deadline = self._clock() + WORKSPACE_WAIT_SECONDS
        resumed = False
        while True:
            state = await sync_to_async(runners.get_workspace_launch_state)(
                workspace_id
            )
            available = await sync_to_async(runners.is_workspace_available)(
                workspace_id
            )
            if state.status in (Status.RUNNING, Status.STOPPED) and (
                state.runner_status != "online"
            ):
                raise _unavailable("The workspace's runner is offline.", 30)
            if (
                state.status == Status.RUNNING
                and not state.active_operation
                and (available)
            ):
                return
            if state.status == Status.STOPPED and not state.active_operation:
                if not resumed:
                    resumed = True
                    await self._resume(workspace_id)
            elif state.status not in (Status.RUNNING, Status.STOPPED, Status.CREATING):
                raise BackendError(
                    f"The workspace is {state.status or 'gone'}.",
                    status=503 if state.status == Status.FAILED else 404,
                    code="backend_workspace_unavailable",
                )
            if self._clock() >= deadline:
                starting = resumed or (
                    state.active_operation == WorkspaceOperation.STARTING
                )
                raise _unavailable(
                    "The workspace is starting; try again in a few seconds."
                    if starting
                    else "The workspace is busy; try again in a few seconds."
                )
            await asyncio.sleep(1.0)

    async def _resume(self, workspace_id: uuid.UUID) -> None:
        async with self._lock("resume", str(workspace_id)):
            state = await sync_to_async(self._runners().get_workspace_launch_state)(
                workspace_id
            )
            if state.status != Status.STOPPED or state.active_operation:
                return  # Another request resumed it meanwhile.
            try:
                await self._runners().resume_workspace(workspace_id)
            except ServiceError as exc:
                log.info(
                    "app_backend_resume_refused",
                    workspace_id=str(workspace_id),
                    error=exc.message,
                )
                raise _unavailable(f"The workspace cannot start: {exc.message}", 30)
            log.info("app_backend_workspace_resumed", workspace_id=str(workspace_id))

    async def _find_process(self, workspace_id: uuid.UUID, label: str) -> Any:
        processes = await self._runners().list_processes(
            workspace_id, kinds=(ProcessKind.PERSISTENT,), live=False
        )
        matches = [p for p in processes if (p.name or "").lower() == label]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise BackendError(
                f"Several processes are named '{label}' (in different case); "
                "rename all but one.",
                status=409,
                code="backend_process_ambiguous",
            )
        names = sorted({p.name for p in processes if p.name})
        available = f" Available: {', '.join(names)}." if names else ""
        raise BackendError(
            f"The workspace has no process named '{label}'. Keep the server "
            f"running with process_start(name='{label}', for_user=true)."
            f"{available}",
            status=404,
            code="backend_process_not_found",
        )

    async def _ensure_process_running(self, workspace_id: uuid.UUID, process: Any):
        if process.status == ProcessStatus.RUNNING and process.pid is not None:
            return process
        async with self._lock("process", str(process.id)):
            current = await self._runners().get_process(workspace_id, process.id)
            # RUNNING without a pid is a start in flight (e.g. an agent's
            # restart); restarting would kill that run. Only a start that
            # never confirms is restarted.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PROCESS_START_WAIT_SECONDS
            while (
                current.status == ProcessStatus.RUNNING
                and current.pid is None
                and loop.time() < deadline
            ):
                await asyncio.sleep(0.5)
                current = await self._runners().get_process(workspace_id, process.id)
            if current.status == ProcessStatus.RUNNING and current.pid is not None:
                return current
            try:
                started = await self._runners().restart_process(
                    workspace_id, process.id
                )
            except ServiceError as exc:
                raise BackendError(
                    f"Could not start process '{process.name}': {exc.message}",
                    status=502,
                    code="backend_process_start_failed",
                ) from exc
            log.info(
                "app_backend_process_started",
                workspace_id=str(workspace_id),
                process_id=str(process.id),
                pid=started.pid,
            )
            if started.pid is None:
                raise _unavailable(f"Process '{process.name}' is starting.", 2)
            return started

    async def _snapshot(self, workspace_id: uuid.UUID, pid: int) -> ListeningSnapshot:
        accessor = await self._accessor_factory(str(workspace_id))
        result = await accessor.exec_wait(LISTENING_COMMAND, timeout=PROBE_TIMEOUT)
        return parse_listening(result.stdout, pid)

    async def _port(
        self, workspace_id: uuid.UUID, process: Any, requested: int | None
    ) -> int:
        """The port to forward to, waiting while the process starts listening."""
        key = (workspace_id, process.pid, requested)
        cached = self._ports.get(key)
        if cached and cached[1] > self._clock():
            return cached[0]
        deadline = self._clock() + PORT_WAIT_SECONDS
        delay = 0.25
        while True:
            try:
                snapshot = await self._snapshot(workspace_id, process.pid)
            except (ServiceError, RuntimeError, TimeoutError) as exc:
                raise _unavailable(f"The workspace did not answer: {exc}", 5) from exc
            port = snapshot.port_for(requested)
            if port is not None:
                self._ports[key] = (port, self._clock() + PORT_CACHE_SECONDS)
                return port
            if snapshot.alive is False:
                current = await self._runners().get_process(workspace_id, process.id)
                if current.status != ProcessStatus.RUNNING:
                    raise _ProcessExitedError(current)
            if self._clock() >= deadline:
                what = f"port {requested}" if requested else "any port"
                raise _unavailable(
                    f"Process '{process.name}' is running but not listening on "
                    f"{what} yet.",
                    3,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 1.0)

    async def _touch(self, workspace_id: uuid.UUID) -> None:
        """Count gateway traffic as activity (throttled per workspace)."""
        now = self._clock()
        if (
            now - self._touched.get(workspace_id, float("-inf"))
            < ACTIVITY_TOUCH_SECONDS
        ):
            return
        self._touched[workspace_id] = now
        try:
            await self._runners().touch_workspace_activity(workspace_id)
        except Exception:  # noqa: BLE001 - activity is best effort
            log.warning("app_backend_touch_failed", workspace_id=str(workspace_id))


_service: AppBackendService | None = None


def get_app_backend_service() -> AppBackendService:
    """The process-wide service (its locks and caches span requests)."""
    global _service
    if _service is None:
        _service = AppBackendService()
    return _service
