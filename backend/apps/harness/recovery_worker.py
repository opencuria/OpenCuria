"""ASGI-owned recovery of persisted harness work after execution is lost."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from .engines.runs import HarnessRunService
from .repositories import HarnessSessionRepository
from .run_recovery_service import RunRecoveryService

log = structlog.get_logger(__name__)


class HarnessRecoveryWorker:
    """Repair turns without requiring a user to submit another prompt.

    Start only after the ASGI scheduler acquires exclusive backend ownership.
    Native execution died with the previous process. External execution instead
    requires expired ownership and positive runner process-release confirmation.
    """

    def __init__(self, *, interval: float = 30.0) -> None:
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self.runs = HarnessRunService()

    async def start(self) -> None:
        """Repair native restart state and supervise external run recovery."""
        if self._task is not None:
            return
        await sync_to_async(RunRecoveryService().recover_backend_restart)()
        self._task = asyncio.create_task(self._run(), name="harness-run-recovery")

    async def stop(self) -> None:
        """Cancel recovery before the backend releases its singleton lease."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _accessor(self, session_id: uuid.UUID) -> Any:
        from apps.runners.services import get_runner_service

        from .access.runner_accessor import create_harness_accessor

        session = await sync_to_async(HarnessSessionRepository.get_by_id)(session_id)
        return await create_harness_accessor(
            get_runner_service(),
            str(session.workspace_id),
            default_timeout=15.0,
            allow_lease_cleanup=True,
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.runs.sweep_stale(
                    self._accessor, on_recovered=self._publish_recovered
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("harness_recovery_iteration_failed")
            await asyncio.sleep(self.interval)

    async def _publish_recovered(self, run_id: uuid.UUID) -> None:
        """Release connected clients from their old busy/waiting presentation."""
        from .harness_service import FRONTEND_EVENT_STATUS, get_harness_service

        row = await sync_to_async(self.runs.repository.get_by_id)(run_id)
        if row is None:
            return
        service = get_harness_service()
        if row.session.status == "idle":
            await service._emit_frontend(
                FRONTEND_EVENT_STATUS,
                service._session_status_payload(
                    row.session,
                    "idle",
                    assistant=row.assistant_message,
                ),
                str(row.session.workspace_id),
            )
        await service.conversation_events.emit(row.session.workspace_id)
