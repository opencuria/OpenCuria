"""One-shot readiness check for release, schema, and worker generation."""

from __future__ import annotations

from django.core.management import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from apps.deployments.models import (
    DatabaseSchemaState,
    LifecycleRecoveryLease,
    Release,
)
from apps.deployments.services import (
    live_generation_manifests,
    live_generation_versions,
    require_compatible_releases,
)
from apps.harness.models import HarnessWorkerRegistration
from apps.runners.services.recovery import RECOVERY_LEASE_KEY
from apps.scheduled_tasks.lease import LEASE_KEY as SCHEDULER_LEASE_KEY
from apps.scheduled_tasks.models import ScheduledTaskSchedulerLease
from common.deployment_compatibility import DeploymentUpgradeError, ReleaseManifest


class Command(BaseCommand):
    """Require an active compatible release and fresh matching worker heartbeat."""

    help = "Check a rollout's database, retained versions, and ready worker"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--release-version", required=True)
        parser.add_argument("--worker-id", required=True)

    def handle(self, *args, **options) -> None:
        if connection.vendor != "postgresql":
            raise CommandError("check_deployment requires PostgreSQL")
        version = options["release_version"]
        release = Release.objects.filter(version=version, state="active").first()
        if release is None:
            raise CommandError(f"Release {version} is not active in the inventory")
        from django.conf import settings

        current = ReleaseManifest.from_mapping(settings.DEPLOYMENT_RELEASE)
        if current.version != version or current != ReleaseManifest(
            version=release.version,
            protocol_min=release.protocol_min,
            protocol_max=release.protocol_max,
            database_schema=release.database_schema,
            database_schema_min=release.database_schema_min,
            database_schema_max=release.database_schema_max,
        ):
            raise CommandError(
                "Gateway metadata does not match active release inventory"
            )
        if version not in live_generation_versions():
            raise CommandError("Gateway release has no live compatible harness worker")
        schema = (
            DatabaseSchemaState.objects.filter(id=1)
            .values_list("schema_version", flat=True)
            .first()
        )
        try:
            generations = live_generation_manifests()
            require_compatible_releases(generations, current_schema=schema)
        except DeploymentUpgradeError as exc:
            raise CommandError(str(exc)) from exc
        worker = HarnessWorkerRegistration.objects.filter(
            worker_id__startswith=options["worker_id"] + "-",
            deployment_version=version,
            state=HarnessWorkerRegistration.State.READY,
            heartbeat_at__gte=timezone.now()
            - HarnessWorkerRegistration.HEARTBEAT_TIMEOUT,
        ).exists()
        if not worker:
            raise CommandError(
                f"No live ready worker {options['worker_id']!r} for {version}"
            )
        recovery = LifecycleRecoveryLease.objects.filter(id=1).first()
        if recovery is None or recovery.owner_token is None:
            raise CommandError(
                "Lifecycle recovery owner/version is not ready for this release"
            )
        self._require_lock(RECOVERY_LEASE_KEY, "Lifecycle recovery")
        scheduler = ScheduledTaskSchedulerLease.objects.filter(key=1).first()
        if scheduler is None or scheduler.owner_token is None:
            raise CommandError("Scheduled-task scheduler is not ready for this release")
        self._require_lock(SCHEDULER_LEASE_KEY, "Scheduled-task scheduler")
        self.stdout.write(
            f"Release {version}, schema {schema}, worker, scheduler, "
            "and recovery are ready"
        )

    def _require_lock(self, key: int, name: str) -> None:
        """Require a session advisory lock held by an open postgres backend."""
        high = (key >> 32) & 0xFFFFFFFF
        low = key & 0xFFFFFFFF
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND granted AND classid=%s::oid AND objid=%s::oid AND objsubid=1)",
                [high, low],
            )
            if not cursor.fetchone()[0]:
                raise CommandError(f"{name} advisory lock is not held")
