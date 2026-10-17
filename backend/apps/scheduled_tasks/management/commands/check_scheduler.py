"""Readiness command for the independently leased scheduler process."""

from __future__ import annotations

from django.core.management import BaseCommand, CommandError
from django.db import connection

from apps.scheduled_tasks.lease import LEASE_KEY, LEASE_ROW_KEY
from apps.scheduled_tasks.models import ScheduledTaskSchedulerLease


class Command(BaseCommand):
    """Require the exact candidate generation to own the scheduler lease."""

    help = "Check scheduler generation readiness before a deployment cutover"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--release-version", required=True)
        parser.add_argument("--owner-id", required=True)

    def handle(self, *args, **options) -> None:
        if connection.vendor != "postgresql":
            raise CommandError("check_scheduler requires PostgreSQL")
        version = options["release_version"]
        row = ScheduledTaskSchedulerLease.objects.filter(key=LEASE_ROW_KEY).first()
        if (
            row is None
            or row.deployment_version != version
            or row.owner_id != options["owner_id"]
            or row.owner_token is None
        ):
            raise CommandError(
                f"Scheduler does not own the expected release lease ({version})"
            )
        high = (LEASE_KEY >> 32) & 0xFFFFFFFF
        low = LEASE_KEY & 0xFFFFFFFF
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND granted AND classid = %s::oid AND objid = %s::oid "
                "AND objsubid = 1)",
                [high, low],
            )
            held = bool(cursor.fetchone()[0])
        if not held:
            raise CommandError(
                "Scheduler lease row exists but its PostgreSQL lock is not held"
            )
        self.stdout.write(f"Scheduler lease held by release {version}")
