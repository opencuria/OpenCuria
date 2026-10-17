"""Gracefully drain one release without terminating owned runs."""

from __future__ import annotations

from django.core.management import BaseCommand, CommandError
from django.db import connection

from apps.deployments.models import DatabaseSchemaState, Release
from apps.deployments.services import (
    live_generation_manifests,
    require_compatible_releases,
)
from apps.harness.models import (
    HarnessRun,
    HarnessWorkerRegistration,
    QuestionRequest,
)
from apps.harness.permissions.models import PermissionRequest


class Command(BaseCommand):
    """Fence root admissions and wait without an arbitrary run/drain timeout."""

    help = "Drain a release's workers and owned runs; never force-kills workers"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--release-version", required=True)
        parser.add_argument("--poll-seconds", type=float, default=5.0)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options) -> None:
        if connection.vendor != "postgresql":
            raise CommandError("drain_release requires PostgreSQL")
        version = options["release_version"]
        if options["poll_seconds"] <= 0:
            raise CommandError("--poll-seconds must be positive")
        try:
            release = Release.objects.get(version=version)
        except Release.DoesNotExist as exc:
            raise CommandError(f"Release {version} is not registered") from exc
        if release.state == "rollback":
            raise CommandError("Rollback-retained releases cannot be drained")
        if release.state != "draining":
            Release.objects.filter(version=version).update(state="draining")
        from apps.harness.worker_protocol import HarnessWorkerRepository

        worker_ids = list(
            HarnessWorkerRegistration.objects.filter(
                deployment_version=version,
                state=HarnessWorkerRegistration.State.READY,
            ).values_list("worker_id", flat=True)
        )
        for worker_id in worker_ids:
            HarnessWorkerRepository.set_draining(worker_id)
        self.stdout.write(
            f"Release {version} marked draining; new root admission is fenced"
        )

        while True:
            active_runs = HarnessRun.objects.filter(
                deployment_version=version,
                status__in=("starting", "running", "closing"),
            )
            worker_rows = list(
                HarnessWorkerRegistration.objects.filter(
                    deployment_version=version,
                    state__in=(
                        HarnessWorkerRegistration.State.READY,
                        HarnessWorkerRegistration.State.DRAINING,
                    ),
                )
            )
            sessions = HarnessRun.objects.filter(
                deployment_version=version
            ).values_list("session_id", flat=True)
            pending_permissions = PermissionRequest.objects.filter(
                session_id__in=sessions, status="pending"
            ).count()
            pending_questions = QuestionRequest.objects.filter(
                session_id__in=sessions, status="pending"
            ).count()
            run_count = active_runs.count()
            worker_count = len(worker_rows)
            self.stdout.write(
                f"release={version} active_runs={run_count} "
                f"active_workers={worker_count} "
                f"pending_permissions={pending_permissions} "
                f"pending_questions={pending_questions}"
            )

            # Worker SIGTERM handling drains in-process tasks, reports STOPPED,
            # and exits normally; the deploy script never stops it automatically.
            if (
                run_count == 0
                and worker_count == 0
                and pending_permissions == 0
                and pending_questions == 0
            ):
                schema = (
                    DatabaseSchemaState.objects.filter(id=1)
                    .values_list("schema_version", flat=True)
                    .first()
                )
                require_compatible_releases(
                    live_generation_manifests(), current_schema=schema
                )
                self.stdout.write(self.style.SUCCESS("Release has drained safely"))
                return
            if options["once"]:
                return
            import asyncio

            asyncio.run(asyncio.sleep(options["poll_seconds"]))
