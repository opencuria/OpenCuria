"""Recover attempts only after an operator proves the exact worker is dead."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandError

from apps.harness.engines.runs import HarnessRunService
from apps.harness.harness_service import get_harness_service
from apps.harness.models import HarnessRun, HarnessWorkerRegistration
from apps.harness.worker_protocol import HarnessWorkerRepository

log = structlog.get_logger(__name__)


class Command(BaseCommand):
    """Attempt proof-based cleanup; never transfer or replay unknown effects."""

    help = "Safely recover harness runs after confirmed worker process death"

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("worker_id")
        parser.add_argument("--confirm-worker-dead", action="store_true")
        parser.add_argument(
            "--confirm-runner-quiescent",
            action="store_true",
            help=(
                "Required for native runs after verifying/cancelling "
                "all their runner operations; "
                "no shell or MCP effect may still be in flight"
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["confirm_worker_dead"]:
            raise CommandError(
                "Pass --confirm-worker-dead only after process death is verified"
            )
        worker_id = options["worker_id"]
        worker = HarnessWorkerRegistration.objects.filter(worker_id=worker_id).first()
        if worker is None:
            raise CommandError(f"Unknown worker {worker_id!r}")
        if worker.state not in {
            HarnessWorkerRegistration.State.STOPPED,
            HarnessWorkerRegistration.State.FAILED,
        }:
            raise CommandError(
                "Mark worker stopped/failed only after process death is proven"
            )
        has_native = HarnessRun.objects.filter(
            owner_worker_id=worker_id,
            harness_id="native",
            status__in=("starting", "running", "closing"),
        ).exists()
        if has_native and not options.get("confirm_runner_quiescent"):
            raise CommandError(
                "Native effects have unknown state; verify runner quiescence and pass "
                "--confirm-runner-quiescent. Never retry the original commands."
            )
        outstanding = HarnessWorkerRepository.recover_dead_worker(
            worker_id, confirmed_dead=True
        )
        log.warning(
            "harness_worker_death_confirmed", worker_id=worker_id, runs=outstanding
        )
        try:
            count = asyncio.run(self._recover(worker_id))
        except Exception as exc:
            raise CommandError(
                "Worker is marked stopped, but cleanup remains unresolved; "
                "fix runner connectivity and retry"
            ) from exc
        self.stdout.write(self.style.SUCCESS(f"Recovered {count} run(s)."))

    async def _recover(self, worker_id: str) -> int:
        service = get_harness_service()
        runs = await sync_to_async(
            lambda: list(
                HarnessRun.objects.filter(
                    owner_worker_id=worker_id,
                    status__in=("starting", "running", "closing"),
                ).values_list("session_id", flat=True)
            ),
            thread_sensitive=True,
        )()
        runner = HarnessRunService()
        recovered = 0
        for session_id in runs:
            session = await sync_to_async(
                service.sessions.get_by_id, thread_sensitive=True
            )(session_id)
            if session is None:
                continue
            accessor = None
            if service._accessor_factory is not None:
                accessor = await service._accessor_factory(str(session.workspace_id))
            await runner.prepare_session(
                session.id, accessor, confirmed_native_quiescent=True
            )
            recovered += 1
        return recovered
