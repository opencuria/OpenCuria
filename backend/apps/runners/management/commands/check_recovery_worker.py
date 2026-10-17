"""Readiness check for the unique lifecycle dispatcher process and generation."""

from __future__ import annotations

from django.core.management import BaseCommand, CommandError
from django.db import connection

from apps.deployments.models import LifecycleRecoveryLease
from apps.runners.services.recovery import RECOVERY_LEASE_KEY


class Command(BaseCommand):
    """Probe recovery lock and its durable owner/version metadata."""

    help = "Check that the expected release owns lifecycle recovery"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--owner-id", required=True)
        parser.add_argument("--release-version", required=True)

    def handle(self, *args, **options) -> None:
        if connection.vendor != "postgresql":
            raise CommandError("check_recovery_worker requires PostgreSQL")
        owner = options["owner_id"]
        version = options["release_version"]
        row = LifecycleRecoveryLease.objects.filter(id=1).first()
        if (
            row is None
            or row.owner_id != owner
            or row.deployment_version != version
            or row.owner_token is None
        ):
            raise CommandError("Recovery owner/version metadata is not ready")
        key_high = (RECOVERY_LEASE_KEY >> 32) & 0xFFFFFFFF
        key_low = RECOVERY_LEASE_KEY & 0xFFFFFFFF
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND granted AND classid = %s::oid AND objid = %s::oid "
                "AND objsubid = 1)",
                [key_high, key_low],
            )
            held = bool(cursor.fetchone()[0])
        if not held:
            raise CommandError("Recovery metadata exists but its PG lock is not held")
        self.stdout.write(f"Lifecycle recovery for {version}/{owner} is ready")
