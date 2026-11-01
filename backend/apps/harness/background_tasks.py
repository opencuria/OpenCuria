"""Explicit supervision of execution-process background work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)


class BackgroundTaskSupervisor:
    """Retain tasks through teardown and close admission with one shutdown owner.

    Domain services decide which work to cancel. The supervisor drains remaining
    persistence/hold-release work, including tasks scheduled by done callbacks.
    It never discovers tasks through event-loop or coroutine introspection.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._shutdown: asyncio.Task[None] | None = None

    @property
    def closing(self) -> bool:
        """Admission stays closed after shutdown begins, including after drain."""
        return self._shutdown is not None

    def track(self, task: asyncio.Task) -> None:
        """Take ownership of a task before the caller can suspend."""
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("harness_supervised_task_failed", error=str(task.exception()))

    async def shutdown(self, settle: Callable[[], Awaitable[None]]) -> None:
        """Join one shutdown even when its caller receives another cancellation."""
        if self._shutdown is None:
            self._shutdown = asyncio.create_task(self._settle_and_drain(settle))
        try:
            await asyncio.shield(self._shutdown)
        except asyncio.CancelledError:
            while not self._shutdown.done():
                try:
                    await asyncio.shield(self._shutdown)
                except asyncio.CancelledError:
                    continue
            # Retrieve a teardown failure without replacing caller cancellation.
            if not self._shutdown.cancelled():
                self._shutdown.exception()
            raise

    async def _settle_and_drain(self, settle: Callable[[], Awaitable[None]]) -> None:
        try:
            await settle()
        finally:
            await self._drain()

    async def _drain(self) -> None:
        # A completion callback can create a finalizer/holder-release task
        # after its source task leaves the set. Yield before declaring drain.
        while True:
            await asyncio.sleep(0)
            pending = tuple(self._tasks)
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
