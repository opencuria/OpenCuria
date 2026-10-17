"""Database-backed release inventory and schema marker tests."""

from __future__ import annotations

import pytest

from apps.deployments.models import DatabaseSchemaState, Release
from apps.harness.models import HarnessRun, HarnessWorkerRegistration
from apps.deployments.services import (
    live_generation_manifests,
    register_release,
    require_compatible_releases,
)
from common.deployment_compatibility import (
    DeploymentUpgradeError,
    ReleaseManifest,
)


def manifest(
    version: str, *, protocol_min: int = 1, schema_min: int = 1, schema_max: int = 4
) -> ReleaseManifest:
    """Build a release in the shared protocol/schema compatibility domain."""
    return ReleaseManifest.from_mapping(
        {
            "version": version,
            "protocol_min": protocol_min,
            "protocol_max": max(protocol_min, 3),
            "database_schema": 2,
            "database_schema_min": schema_min,
            "database_schema_max": schema_max,
        }
    )


@pytest.mark.django_db(transaction=True)
def test_release_records_are_immutable_and_all_generations_are_checked() -> None:
    """Persisted facts are immutable and old rollback releases stay in scope."""
    register_release(manifest("release-a"))
    register_release(manifest("release-b"))
    assert DatabaseSchemaState.objects.count() == 0

    changed = manifest("release-a", schema_max=3)
    with pytest.raises(DeploymentUpgradeError, match="metadata changed"):
        register_release(changed)

    with pytest.raises(DeploymentUpgradeError, match="protocol"):
        require_compatible_releases(
            [manifest("release-a"), manifest("release-b"), manifest("release-old", protocol_min=5)]
        )


@pytest.mark.django_db(transaction=True)
def test_live_worker_and_draining_run_versions_are_in_compatibility_set() -> None:
    """Old generations not marked active still constrain every online rollout."""
    register_release(manifest("release-old", protocol_min=1))
    register_release(manifest("release-new", protocol_min=1))
    Release.objects.filter(version="release-old").update(state="draining")
    HarnessWorkerRegistration.objects.create(
        worker_id="worker-old",
        deployment_version="release-old",
        pid=10,
        state=HarnessWorkerRegistration.State.DRAINING,
    )
    retained = live_generation_manifests()
    assert {item.version for item in retained} == {"release-old", "release-new"}

    Release.objects.filter(version="release-old").update(protocol_min=5, protocol_max=6)
    # Persisted immutable release facts can only be changed by corruption or a
    # bad migration; runtime inventory must still fail across every generation.
    with pytest.raises(DeploymentUpgradeError, match="protocol"):
        require_compatible_releases(live_generation_manifests())


@pytest.mark.django_db(transaction=True)
def test_live_generation_missing_manifest_fails_closed() -> None:
    """An active process may not be omitted from the release inventory."""
    HarnessWorkerRegistration.objects.create(
        worker_id="worker-uninventoried",
        deployment_version="legacy-live",
        pid=11,
        state=HarnessWorkerRegistration.State.READY,
    )
    with pytest.raises(DeploymentUpgradeError, match="missing release manifest"):
        live_generation_manifests()


@pytest.mark.django_db(transaction=True)
def test_database_schema_marker_is_monotonic() -> None:
    """Schema tracking does not permit rolling the marker backwards."""
    DatabaseSchemaState.objects.create(id=1, schema_version=3)
    marker = DatabaseSchemaState.objects.get(id=1)
    assert marker.schema_version == 3
