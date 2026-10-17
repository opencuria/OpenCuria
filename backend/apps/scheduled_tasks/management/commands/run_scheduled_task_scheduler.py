"""Run the separately supervised scheduled-task dispatcher."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError

from apps.harness.harness_service import get_harness_service
from apps.harness.worker_protocol import HarnessDeployment
from apps.scheduled_tasks.services import (
    ScheduledTaskScheduler,
    ScheduledTaskService,
)
from common.systemd import notify_systemd

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    """Hold the scheduler lease in a standalone process until graceful shutdown."""

    help = "Run the single-leased scheduled-task dispatcher outside the ASGI gateway"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--interval", type=float, default=5.0)
        parser.add_argument("--owner-id", default="local")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["interval"] < 0.1 or options["interval"] > 60:
            raise CommandError("--interval must be in 0.1..60 seconds")
        deployment = HarnessDeployment.from_environment()
        if os.getenv("OPENCURIA_SCHEDULER_ENABLED", "true").lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise CommandError("OPENCURIA_SCHEDULER_ENABLED is false")
        if deployment.mode == "worker":
            raise CommandError(
                "Scheduler needs a gateway/local Socket.IO transport; "
                "OPENCURIA_HARNESS_MODE=worker is not supported"
            )
        version = deployment.version if deployment.mode != "local" else ""
        owner_id = options.get("owner_id", "local")
        service = ScheduledTaskService(
            harness=get_harness_service(), deployment_version=version
        )
        try:
            scheduler = ScheduledTaskScheduler(
                service,
                interval=options["interval"],
                deployment_version=version,
                owner_id=owner_id,
            )
        except TypeError:
            scheduler = ScheduledTaskScheduler(
                service, interval=options["interval"], deployment_version=version
            )
            scheduler.owner_id = owner_id
        try:
            asyncio.run(self._serve(scheduler, deployment))
        except KeyboardInterrupt:
            self.stdout.write("Scheduler stopped.")

    async def _serve(
        self, scheduler: ScheduledTaskScheduler, deployment: HarnessDeployment
    ) -> None:
        """Start after DB/Redis checks and stop/release the lease on signals."""
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop.set)

        await scheduler.start()
        logger.info(
            "scheduled_task_scheduler_ready",
            deployment_mode=deployment.mode,
            deployment_version=deployment.version,
        )
        notify_systemd("READY=1\nWATCHDOG=1\nSTATUS=Scheduled-task scheduler ready")
        try:
            await stop.wait()
        finally:
            await scheduler.stop()
            from apps.runners.sio_server import get_runner_service

            client = getattr(get_runner_service(), "_harness_runner_rpc_client", None)
            if client is not None:
                await client.close()
            notify_systemd("STOPPING=1\nSTATUS=Scheduled-task scheduler stopped")
