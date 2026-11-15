"""Run agents and supervised jobs independently from HTTP/Socket.IO workers."""

from __future__ import annotations

import asyncio
import signal

from django.core.management.base import BaseCommand

from apps.harness.harness_service import (
    configure_execution_service,
    create_default_harness_service,
)
from config.execution import create_execution_worker


class Command(BaseCommand):
    """Own the execution lifetime for one database and its durable command queue."""

    help = "Run the independent harness execution worker"

    def handle(self, *args, **options) -> None:
        """Keep API lifetimes separate from agent and background-job ownership."""
        asyncio.run(self._run())

    async def _run(self) -> None:
        service = create_default_harness_service()
        configure_execution_service(service)
        worker = create_execution_worker(service)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopped.set)
        try:
            await worker.start()
            await stopped.wait()
        finally:
            await worker.stop()
