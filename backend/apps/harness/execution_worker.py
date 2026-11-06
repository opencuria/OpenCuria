"""Independent execution lifetime for native agents and supervised background jobs."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

import structlog
from asgiref.sync import sync_to_async

from common.exceptions import ServiceError
from common.process_ownership import ExecutionProcessGuard, execution_process_lock_path

from .command_models import (
    ADMISSION_COMMAND_KINDS,
    CONTROL_COMMAND_KINDS,
    EXECUTION_HEARTBEAT_SECONDS,
    HarnessCommand,
)
from .command_repository import (
    ExecutionHeartbeatRepository,
    HarnessCommandRepository,
    SteeringSnapshotRepository,
)
from .command_service import HarnessCommandDispatcher

if TYPE_CHECKING:
    from .harness_service import HarnessService

log = structlog.get_logger(__name__)


async def _start_transport() -> None:
    from apps.runners.reply_routing import start_runner_reply_relay
    from common.realtime import get_sio_server

    get_sio_server().manager.initialize()
    await start_runner_reply_relay()


async def _stop_transport() -> None:
    from apps.runners.reply_routing import stop_runner_reply_relay

    await stop_runner_reply_relay()


class BackgroundWorker(Protocol):
    """Lifecycle of a separately composed background-job collaborator."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class ExecutionScheduler(Protocol):
    """Scheduler startup must repair interrupted runs before dispatch."""

    async def start(
        self, *, before_dispatch: Callable[[], Awaitable[None]]
    ) -> None: ...
    async def stop(self) -> None: ...


class ExecutionWorker:
    """Own task memory and claim durable API commands under exclusive ownership."""

    def __init__(
        self,
        service: HarnessService,
        *,
        repository: type[HarnessCommandRepository] = HarnessCommandRepository,
        heartbeats: type[ExecutionHeartbeatRepository] = ExecutionHeartbeatRepository,
        guard: ExecutionProcessGuard | None = None,
        scheduler: ExecutionScheduler,
        notifications: BackgroundWorker,
        recovery: BackgroundWorker,
        start_transport: Callable[[], Awaitable[None]] = _start_transport,
        stop_transport: Callable[[], Awaitable[None]] = _stop_transport,
        interval: float = 0.05,
        heartbeat_interval: float = EXECUTION_HEARTBEAT_SECONDS,
        max_admissions: int = 8,
        max_controls: int = 32,
    ) -> None:
        if max_admissions < 1 or max_controls < 1:
            raise ValueError("Execution command capacities must be positive")
        self.max_admissions = max_admissions
        self.max_controls = max_controls
        self.service = service
        self.repository = repository
        self.heartbeats = heartbeats
        self.heartbeat_interval = heartbeat_interval
        self.guard = guard or ExecutionProcessGuard(execution_process_lock_path())
        self.scheduler = scheduler
        self.notifications = notifications
        self.recovery = recovery
        self.owner_id = uuid.uuid4()
        self.dispatcher = HarnessCommandDispatcher(service)
        self.start_transport = start_transport
        self.stop_transport = stop_transport
        self.interval = interval
        self._poller: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None
        self._commands: dict[asyncio.Task, HarnessCommand] = {}
        self._started = False

    async def start(self) -> None:
        """Acquire execution ownership and repair old effects before accepting work."""
        if self._started:
            return
        self.guard.acquire()
        try:
            await self.start_transport()
            await self.scheduler.start(before_dispatch=self._recover)
            await self.notifications.start()
            # Publish presence before claiming so API waiters see a live owner.
            await sync_to_async(self.heartbeats.beat)(self.owner_id)
            self._heartbeat = asyncio.create_task(
                self._beat(), name="harness-execution-heartbeat"
            )
            self._poller = asyncio.create_task(
                self._run(), name="harness-command-worker"
            )
            self._started = True
        except BaseException:
            await self.notifications.stop()
            await self.recovery.stop()
            await self.scheduler.stop()
            await self.stop_transport()
            # Retain process ownership: startup may already have dispatched tasks.
            raise

    async def _recover(self) -> None:
        """A claimed command is ambiguous after a crash and must never be replayed."""
        await self.recovery.start()
        await sync_to_async(self.repository.interrupt_claimed)()
        await sync_to_async(SteeringSnapshotRepository.clear)()
        await sync_to_async(self.heartbeats.prune)()

    async def stop(self) -> None:
        """Stop admission and supervised jobs, then settle the worker's task trees."""
        if self._poller is not None:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
            self._poller = None
        await self.scheduler.stop()
        await self.notifications.stop()
        await self.recovery.stop()
        # Commands are independently supervised so an abort/question can proceed
        # while another request is awaiting workspace or provider preparation.
        await self._cancel_commands(tuple(self._commands.items()))
        # Every claimed command has its reply now; queued ones fail fast instead
        # of waiting for this lifetime while native runs drain.
        await self._stop_heartbeat()
        await self.service.shutdown_runs()
        await self.stop_transport()
        self._started = False
        # Process exclusion deliberately lasts until exit, including cleanup.

    async def _beat(self) -> None:
        """Renew presence for as long as this lifetime accepts commands."""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await sync_to_async(self.heartbeats.beat)(self.owner_id)
            except Exception:
                log.exception("harness_execution_heartbeat_failed")

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat is None:
            return
        self._heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._heartbeat
        self._heartbeat = None
        try:
            await sync_to_async(self.heartbeats.remove)(self.owner_id)
        except Exception:
            # The presence expires on its own; shutdown must still settle runs.
            log.exception("harness_execution_heartbeat_remove_failed")

    async def _run(self) -> None:
        while True:
            try:
                admission_count = sum(
                    row.kind in ADMISSION_COMMAND_KINDS
                    for row in self._commands.values()
                )
                control_count = len(self._commands) - admission_count
                allowed: set[str] = set()
                if admission_count < self.max_admissions:
                    allowed.update(ADMISSION_COMMAND_KINDS)
                if control_count < self.max_controls:
                    allowed.update(CONTROL_COMMAND_KINDS)
                if not allowed:
                    await asyncio.sleep(self.interval)
                    continue
                claim = asyncio.create_task(
                    sync_to_async(self.repository.claim)(
                        self.owner_id, allowed_kinds=allowed
                    )
                )
                try:
                    row = await asyncio.shield(claim)
                except asyncio.CancelledError:
                    # A committing claim cannot be abandoned between storage and
                    # task creation, even during graceful process shutdown.
                    while not claim.done():
                        try:
                            await asyncio.shield(claim)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    try:
                        row = claim.result()
                    except Exception:
                        log.exception("harness_claim_failed_during_shutdown")
                        raise asyncio.CancelledError from None
                    if row is not None:
                        self._launch_command(row)
                    raise
                if row is None:
                    await asyncio.sleep(self.interval)
                    continue
                self._launch_command(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("harness_command_poll_failed")
                await asyncio.sleep(self.interval)

    def _launch_command(self, row: HarnessCommand) -> None:
        task = asyncio.create_task(self._execute(row), name=f"harness-command:{row.id}")
        self._commands[task] = row
        task.add_done_callback(self._commands.pop)

    async def _cancel_commands(
        self, commands: tuple[tuple[asyncio.Task, HarnessCommand], ...]
    ) -> bool:
        """Join cancellation/rollback once, including tasks cancelled before entry."""
        for task, _ in commands:
            if not task.done() and not task.cancelling():
                task.cancel()
        if commands:
            joined = asyncio.gather(
                *(task for task, _ in commands), return_exceptions=True
            )
            # A control or shutdown waiter must not cancel an admission again
            # while that admission is rolling back or persisting its outcome.
            while not joined.done():
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError:
                    continue
            for task, row in commands:
                if task.cancelled():
                    await self._complete(row, self._interrupted_result())
        owner = asyncio.current_task()
        return owner is not None and bool(owner.cancelling())

    async def _settle_earlier_admissions(self, control: HarnessCommand) -> None:
        """Close the claim-to-entry gap before a same-session stop or deletion."""
        earlier = tuple(
            (task, row)
            for task, row in self._commands.items()
            if row.session_id == control.session_id
            and row.kind in ADMISSION_COMMAND_KINDS
            and (row.created_at, row.id.int) < (control.created_at, control.id.int)
        )
        if earlier:
            # Preserve explicit stop provenance even when the admission has
            # already created its assistant shell but has not returned its reply.
            await self.service.request_stop(control.session_id)
            if await self._cancel_commands(earlier):
                raise asyncio.CancelledError

    async def _execute(self, row: HarnessCommand) -> None:
        try:
            if row.kind in {"abort", "delete"} and row.session_id is not None:
                await self._settle_earlier_admissions(row)
            payload = self.repository.decode_payload(row)
            result = {
                "ok": True,
                "value": await self.dispatcher.execute(row.kind, payload),
            }
        except asyncio.CancelledError:
            result = self._interrupted_result()
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "code": getattr(exc, "code", "execution_failed"),
                "message": str(exc)
                if isinstance(exc, (ServiceError, ValueError))
                else "Execution request failed",
            }
            if hasattr(exc, "resource"):
                error.update(resource=exc.resource, identifier=exc.identifier)
            result = {"ok": False, "error": error}
            if not isinstance(exc, (ServiceError, ValueError)):
                log.exception("harness_command_failed", command_id=str(row.id))
        await self._complete(row, result)

    @staticmethod
    def _interrupted_result() -> dict:
        return {
            "ok": False,
            "error": {
                "type": "ConflictError",
                "code": "execution_interrupted",
                "message": "Execution request was interrupted before completion",
            },
        }

    async def _complete(self, row: HarnessCommand, result: dict) -> None:
        """Retain the original outcome through cancellation of writes or backoff."""
        completion = asyncio.create_task(self._persist_result(row, result))
        # The worker owns this terminal outcome even if stop() cancels its
        # command waiter. Shield the whole retry lifetime, including sleeps.
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                continue
        completion.result()

    async def _persist_result(self, row: HarnessCommand, result: dict) -> None:
        """Retry only persistence; effects and outcome selection run exactly once."""
        while True:
            try:
                await sync_to_async(self.repository.complete)(
                    row.id, self.owner_id, result
                )
                return
            except Exception:
                log.exception(
                    "harness_command_completion_failed", command_id=str(row.id)
                )
                await asyncio.sleep(self.interval)
