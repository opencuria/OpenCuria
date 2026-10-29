"""ASGI-owned delivery loop; queue state and leases remain in the database."""

from __future__ import annotations

import asyncio
import contextlib
import time

import structlog

from .providers import PushProviders
from .services import NotificationService

log = structlog.get_logger(__name__)


class NotificationWorker:
    """Process durable jobs without extending the agent's tool or streaming loop."""

    def __init__(self, *, service: NotificationService | None = None) -> None:
        self.service = service
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start exactly once alongside the existing ASGI scheduler."""
        if self._task is not None:
            return
        if self.service is None:
            self.service = NotificationService(providers=PushProviders())
        self._task = asyncio.create_task(self._run(), name="mobile-notifications")

    async def stop(self) -> None:
        """Cancel safely; claimed jobs become eligible again after their lease."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.service and self.service.providers:
            await self.service.providers.close()

    async def _run(self) -> None:
        last_repair = 0.0
        last_heartbeat = 0.0
        while True:
            try:
                if time.monotonic() - last_repair >= 60:
                    await self.service.repair()
                    last_repair = time.monotonic()
                if time.monotonic() - last_heartbeat >= 300:
                    await self.service.record_heartbeat()
                    last_heartbeat = time.monotonic()
                worked = await self.service.process_one()
                await asyncio.sleep(0 if worked else 1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("mobile_notification_worker_iteration_failed")
                await asyncio.sleep(5)
