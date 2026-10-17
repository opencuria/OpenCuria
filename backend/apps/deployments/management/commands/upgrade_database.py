"""Single-lock online migration entry point with a migration safety gate."""

from __future__ import annotations

import time

from django.core.management import BaseCommand, CommandError, call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.deployments.services import (
    live_generation_manifests,
    register_release,
    require_compatible_releases,
)
from common.deployment_compatibility import (
    DeploymentUpgradeError,
    ReleaseManifest,
    migration_safety_error,
)

LOCK_KEY = 0x4F43555044415445  # "OCUPDATE", stable across all migration jobs.
LOCK_WAIT_SECONDS = 30


class Command(BaseCommand):
    """Validate and apply safe online migrations, or fail with review guidance."""

    help = (
        "Run compatibility-checked online migrations under a PostgreSQL advisory lock"
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--manifest", required=True, help="Candidate release manifest JSON"
        )
        parser.add_argument("--lock-timeout", type=int, default=LOCK_WAIT_SECONDS)
        parser.add_argument(
            "--adopt-existing-release-manifest",
            help="One-time manifest for the currently running legacy release",
        )
        parser.add_argument(
            "--allow-initial-maintenance-bootstrap",
            action="store_true",
            help="Permit historical migrations on a fresh DB in explicit maintenance",
        )
        parser.add_argument(
            "--plan-only", action="store_true", help="Validate without migrating"
        )

    def handle(self, *args, **options) -> None:
        if connection.vendor != "postgresql":
            raise CommandError(
                "upgrade_database requires PostgreSQL; legacy migrate is unchanged"
            )
        try:
            candidate = ReleaseManifest.from_file(options["manifest"])
        except DeploymentUpgradeError as exc:
            raise CommandError(str(exc)) from exc
        from django.conf import settings

        candidate_matches_runtime = candidate == ReleaseManifest.from_mapping(
            settings.DEPLOYMENT_RELEASE
        )
        if not candidate_matches_runtime:
            raise CommandError(
                "Container release metadata does not exactly match /deploy/release.json"
            )
        if options["lock_timeout"] < 0:
            raise CommandError("--lock-timeout must be non-negative")

        self._acquire_lock(options["lock_timeout"])
        try:
            existing_tables = set(connection.introspection.table_names())
            has_django_history = "django_migrations" in existing_tables
            if has_django_history and "deployments_release" not in existing_tables:
                if not options.get("adopt_existing_release_manifest"):
                    raise CommandError(
                        "Existing database needs an exact current-release manifest "
                        "for one-time adoption before inventory schema changes."
                    )
                if options["plan_only"]:
                    raise CommandError(
                        "Inventory adoption requires a separate maintenance step; "
                        "plan-only made no changes"
                    )
                call_command("migrate", "deployments", interactive=False, verbosity=0)
            if options["allow_initial_maintenance_bootstrap"] and has_django_history:
                raise CommandError(
                    "Maintenance bootstrap is only allowed on an empty database"
                )
            self._upgrade(candidate, options)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])

    def _acquire_lock(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
                locked = cursor.fetchone()[0]
            if locked:
                return
            if time.monotonic() >= deadline:
                raise CommandError(
                    f"Timed out after {timeout}s waiting for migration advisory lock"
                )
            time.sleep(min(1, max(0.05, deadline - time.monotonic())))

    def _upgrade(self, candidate: ReleaseManifest, options: dict) -> None:
        from apps.deployments.models import DatabaseSchemaState

        existing_tables = set(connection.introspection.table_names())
        inventory_ready = "deployments_release" in existing_tables
        prior = tuple(live_generation_manifests()) if inventory_ready else ()
        legacy_path = options.get("adopt_existing_release_manifest")
        legacy: ReleaseManifest | None = None
        if not prior and legacy_path:
            try:
                legacy = ReleaseManifest.from_file(legacy_path)
                if legacy.version == candidate.version:
                    prior = (legacy,)
                elif options["plan_only"]:
                    prior = (legacy,)
                else:
                    # Adoption is a separate no-DDL deployment step. The next
                    # call sees the now-registered current release and applies
                    # the candidate migration plan under the same safety gate.
                    require_compatible_releases((legacy, candidate))
                    register_release(legacy)
                    if "deployments_databaseschemastate" in existing_tables:
                        DatabaseSchemaState.objects.get_or_create(
                            id=1, defaults={"schema_version": legacy.database_schema}
                        )
                    self.stdout.write(
                        f"Adopted current release {legacy.version}; rerun "
                        f"the upgrade for {candidate.version}."
                    )
                    return
            except DeploymentUpgradeError as exc:
                raise CommandError(str(exc)) from exc
        current_schema = None
        if "deployments_databaseschemastate" in existing_tables:
            current_schema = (
                DatabaseSchemaState.objects.filter(id=1)
                .values_list("schema_version", flat=True)
                .first()
            )
        if prior and current_schema is None:
            current_schema = prior[-1].database_schema
        if current_schema is not None and not (
            candidate.database_schema_min
            <= current_schema
            <= candidate.database_schema_max
        ):
            raise CommandError(
                f"Release {candidate.version} does not support current schema "
                f"{current_schema}; do not rollback application code onto this DB."
            )
        if prior:
            try:
                require_compatible_releases(
                    (*prior, candidate),
                    current_schema=max(current_schema or 0, candidate.database_schema),
                )
            except DeploymentUpgradeError as exc:
                raise CommandError(str(exc)) from exc
        elif not options["allow_initial_maintenance_bootstrap"]:
            raise CommandError(
                "No release is registered. Supply the exact live legacy manifest with "
                "--adopt-existing-release-manifest, or use the explicit fresh-DB "
                "maintenance bootstrap."
            )

        if legacy and not inventory_ready:
            if legacy.version != candidate.version:
                if not options["plan_only"]:
                    register_release(legacy)
                if "deployments_databaseschemastate" not in existing_tables:
                    DatabaseSchemaState.objects.create(
                        id=1, schema_version=legacy.database_schema
                    )
                else:
                    DatabaseSchemaState.objects.get_or_create(
                        id=1, defaults={"schema_version": legacy.database_schema}
                    )
                self.stdout.write(
                    f"{'Would adopt' if options['plan_only'] else 'Adopted'} current "
                    f"release {legacy.version}; rerun upgrade for {candidate.version} "
                    "after reviewing the migration plan."
                )
                return
            if options["plan_only"]:
                self.stdout.write(f"Would adopt current release {legacy.version}")
                return
            register_release(legacy)
            if "deployments_databaseschemastate" not in existing_tables:
                DatabaseSchemaState.objects.create(
                    id=1, schema_version=legacy.database_schema
                )
            else:
                DatabaseSchemaState.objects.get_or_create(
                    id=1, defaults={"schema_version": legacy.database_schema}
                )
            self.stdout.write(f"Adopted current release {legacy.version}")
            return

        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        maintenance_bootstrap = (
            not prior and options["allow_initial_maintenance_bootstrap"]
        )
        unsafe = [
            error
            for migration, backwards in plan
            if not backwards and (error := migration_safety_error(migration))
        ]
        if unsafe and not maintenance_bootstrap:
            raise CommandError(
                "Online migration refused: "
                + "; ".join(unsafe)
                + ". Use reviewed expand/backfill/contract changes; opaque SQL/Python "
                "requires an audited migration-specific rationale."
            )
        if options["plan_only"]:
            self.stdout.write(
                f"Validated {len(plan)} migration operation(s); no changes made."
            )
            return

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '2s'")
            call_command("migrate", interactive=False, verbosity=options["verbosity"])
        except Exception as exc:
            raise CommandError(
                "Migration failed; DB and old release were not reversed. "
                "Inspect Django "
                "migration state and PostgreSQL error. A failed concurrent index may "
                "remain invalid: inspect it, drop that exact index CONCURRENTLY, "
                "then retry. "
                "Never mark failed operations applied blindly."
            ) from exc

        state = DatabaseSchemaState.objects.filter(id=1).first()
        if state is None:
            DatabaseSchemaState.objects.create(
                id=1, schema_version=candidate.database_schema
            )
        elif candidate.database_schema > state.schema_version:
            state.schema_version = candidate.database_schema
            state.save(update_fields=("schema_version", "updated_at"))
        try:
            register_release(candidate)
        except DeploymentUpgradeError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(f"Database ready for release {candidate.version}")
        )
