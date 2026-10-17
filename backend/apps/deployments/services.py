"""Compatibility policy for immutable backend deployment releases."""

from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from common.deployment_compatibility import DeploymentUpgradeError, ReleaseManifest


def require_compatible_releases(
    releases: Iterable[ReleaseManifest], *, current_schema: int | None = None
) -> None:
    """Require every active, draining, and rollback-retained release to agree.

    Protocol ranges must share a non-empty intersection. DB compatibility is
    checked against each binary's supported schema interval and the monotonic
    current-schema marker, not by requiring a shared origin schema.
    """
    retained = tuple(releases)
    if not retained:
        raise DeploymentUpgradeError("At least one active release is required")
    if current_schema is not None:
        incompatible = [
            release.version
            for release in retained
            if not release.database_schema_min
            <= current_schema
            <= release.database_schema_max
        ]
        if incompatible:
            raise DeploymentUpgradeError(
                f"Current database schema {current_schema} is unsupported by retained "
                f"release(s): {', '.join(incompatible)}"
            )
    protocol_min = max(release.protocol_min for release in retained)
    protocol_max = min(release.protocol_max for release in retained)
    if protocol_min > protocol_max:
        versions = ", ".join(release.version for release in retained)
        raise DeploymentUpgradeError(
            f"Retained releases have no shared worker protocol ({versions})"
        )


def register_release(manifest: ReleaseManifest, *, state: str = "active") -> None:
    """Record release metadata immutably; serialize first registration/update."""
    from .models import Release

    if state not in {"active", "draining", "rollback"}:
        raise DeploymentUpgradeError(f"Invalid retained release state: {state}")
    defaults = {
        "protocol_min": manifest.protocol_min,
        "protocol_max": manifest.protocol_max,
        "database_schema": manifest.database_schema,
        "database_schema_min": manifest.database_schema_min,
        "database_schema_max": manifest.database_schema_max,
        "state": state,
        "retired_at": None,
    }
    with transaction.atomic():
        # A missing-row SELECT FOR UPDATE does not serialize first writers.
        # Take a per-release transaction lock before checking/inserting.
        from django.db import connection

        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    [manifest.version],
                )
        release = (
            Release.objects.select_for_update().filter(version=manifest.version).first()
        )
        if release is None:
            Release.objects.create(version=manifest.version, **defaults)
            return
        actual = (
            release.protocol_min,
            release.protocol_max,
            release.database_schema,
            release.database_schema_min,
            release.database_schema_max,
        )
        expected = (
            manifest.protocol_min,
            manifest.protocol_max,
            manifest.database_schema,
            manifest.database_schema_min,
            manifest.database_schema_max,
        )
        if actual != expected:
            raise DeploymentUpgradeError(
                f"Release {manifest.version} metadata changed; identifiers are "
                "immutable"
            )
        release.state = state
        release.retired_at = None
        release.save(update_fields=("state", "retired_at"))


def retire_release(version: str) -> None:
    """Retire release after its workers/runs drained and rollback retention ended."""
    from .models import Release

    changed = (
        Release.objects.filter(version=version)
        .exclude(state="retired")
        .update(state="retired", retired_at=timezone.now())
    )
    if (
        not changed
        and not Release.objects.filter(version=version, state="retired").exists()
    ):
        raise DeploymentUpgradeError(f"Release {version} is not registered")


def retained_manifests() -> tuple[ReleaseManifest, ...]:
    """Read all releases that still need a common protocol/schema contract."""
    from .models import Release

    return tuple(
        ReleaseManifest(
            version=row.version,
            protocol_min=row.protocol_min,
            protocol_max=row.protocol_max,
            database_schema=row.database_schema,
            database_schema_min=row.database_schema_min,
            database_schema_max=row.database_schema_max,
        )
        for row in Release.objects.exclude(state="retired")
    )


def live_generation_versions() -> frozenset[str]:
    """Return every version with a live worker registration or unresolved run."""
    from apps.harness.models import HarnessRun, HarnessWorkerRegistration

    versions = set(
        HarnessWorkerRegistration.objects.filter(
            state__in=(
                HarnessWorkerRegistration.State.READY,
                HarnessWorkerRegistration.State.DRAINING,
            )
        ).values_list("deployment_version", flat=True)
    )
    versions.update(
        HarnessRun.objects.filter(
            status__in=("starting", "running", "closing")
        )
        .exclude(deployment_version="")
        .values_list("deployment_version", flat=True)
    )
    return frozenset(version for version in versions if version)


def live_generation_manifests() -> tuple[ReleaseManifest, ...]:
    """Include every version that owns a live worker or unresolved run.

    The persisted release inventory is authoritative for compatibility ranges,
    but it can be incomplete while adopting a legacy installation. Runtime
    worker/run records close that gap: a live version absent from inventory (or
    marked retired) makes an online rollout fail closed.
    """
    from .models import Release

    rows = {row.version: row for row in Release.objects.all()}
    live_versions = set(live_generation_versions())

    missing = sorted(version for version in live_versions if version not in rows)
    retired = sorted(
        version
        for version in live_versions
        if version in rows and rows[version].state == "retired"
    )
    if missing or retired:
        details = []
        if missing:
            details.append(f"missing release manifest: {', '.join(missing)}")
        if retired:
            details.append(f"live generation marked retired: {', '.join(retired)}")
        raise DeploymentUpgradeError("Unsafe live generation inventory (" + "; ".join(details) + ")")

    versions = {
        row.version
        for row in rows.values()
        if row.state != "retired" or row.version in live_versions
    }
    return tuple(
        ReleaseManifest(
            version=row.version,
            protocol_min=row.protocol_min,
            protocol_max=row.protocol_max,
            database_schema=row.database_schema,
            database_schema_min=row.database_schema_min,
            database_schema_max=row.database_schema_max,
        )
        for row in (rows[version] for version in sorted(versions))
    )
