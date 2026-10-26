"""Characterize the cached storage read model independently of scan intake."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.runners.inventory_repository import InventoryRepository
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
    InventoryRuntime,
    InventorySnapshot,
)
from apps.runners.services.inventory_inspection import InventoryInspectionService
from apps.runners.tests.conftest import capture_version_fields
from apps.runners.tests.test_inventory_persistence import scan

pytestmark = pytest.mark.django_db


def test_inspection_without_observations_preserves_empty_contract(runner) -> None:
    """Even an unscanned runner has one unknown runtime and all workflow lists."""
    assert InventoryInspectionService.inspect(runner) == {
        "runner_id": str(runner.id),
        "runner_online": True,
        "latest_snapshot_id": None,
        "latest_complete": False,
        "runtimes": [
            {
                "fresh": False,
                "snapshot_id": None,
                "epoch": None,
                "sequence": None,
                "collected_at": None,
                "received_at": None,
                "filesystems": [],
                "resources": [],
                "diagnostics": None,
            }
        ],
        "generations": [],
        "pin_diagnostics": [],
        "operations": [],
        "capture_requests": [],
        "recreate_requests": [],
        "deletions": [],
    }


@pytest.mark.parametrize(
    "age_seconds,expected", [(299, True), (300, False), (-1, False)]
)
def test_inspection_freshness_uses_both_clocks(
    runner, monkeypatch, age_seconds: int, expected: bool
) -> None:
    """Old or future collected timestamps never count as current evidence."""
    assert InventoryRepository.record(str(runner.id), runner.sid, scan())
    now = timezone.now()
    InventorySnapshot.objects.update(received_at=now)
    InventoryRuntime.objects.update(collected_at=now - timedelta(seconds=age_seconds))
    monkeypatch.setattr(timezone, "now", lambda: now)

    detail = InventoryInspectionService.inspect(runner)
    assert detail["runtimes"][0]["fresh"] is expected


def test_inspection_never_reconciles_or_writes(runner, workspace) -> None:
    """Reading unproven physical ownership diagnoses it without repairing pins."""
    assert InventoryRepository.record(
        str(runner.id),
        runner.sid,
        scan(
            resources=[
                {
                    "resource_id": "domain",
                    "kind": "workspace",
                    "managed": True,
                    "metadata": {"workspace_id": str(workspace.id)},
                }
            ]
        ),
    )
    with CaptureQueriesContext(connection) as queries:
        detail = InventoryInspectionService.inspect(runner)

    assert queries.captured_queries
    assert all(query["sql"].startswith("SELECT") for query in queries)
    assert detail["pin_diagnostics"] == [
        {
            "workspace_id": str(workspace.id),
            "diagnostic": "Legacy dependency unproven or ambiguous; no pin guessed",
        }
    ]
    workspace.refresh_from_db()
    assert workspace.base_image_instance_id is None


def test_repository_inputs_project_without_lazy_orm_queries(
    runner, workspace, user, monkeypatch, django_assert_num_queries
) -> None:
    """Once repository ports return, even related display fields are in memory."""
    from apps.runners.activity_repository import ActivityRepository
    from apps.runners.image_lines import ImageLineRepository
    from apps.runners.retention_repository import RetentionRepository

    definition = ImageDefinition.objects.create(
        name="Build line", organization=runner.organization
    )
    job = ImageBuildJob.objects.create(runner=runner, image_definition=definition)
    built = ImageInstance.objects.create(
        runner=runner,
        build_job=job,
        origin_definition=definition,
        name="Built",
        created_by=user,
        is_legacy=True,
        runner_ref="base",
    )
    captured = ImageInstance.objects.create(
        runner=runner,
        name="Captured",
        created_by=user,
        runner_ref="captured",
        **capture_version_fields(runner, user, "Captured line"),
    )
    workspace.base_image_instance = built
    workspace.pending_base_image_instance = captured
    workspace.save()
    assert InventoryRepository.record(
        str(runner.id),
        runner.sid,
        scan(
            resources=[
                {"resource_id": "base", "kind": "image", "managed": True},
                {
                    "resource_id": "disk",
                    "kind": "disk",
                    "managed": True,
                    "allocated_bytes": 123,
                    "aliases": ["disk-alias"],
                    "dependencies": ["base"],
                    "metadata": {"workspace_id": str(workspace.id)},
                },
            ]
        ),
    )
    data = InventoryRepository.load_inspection(runner)
    monkeypatch.setattr(InventoryRepository, "load_inspection", lambda _: data)
    monkeypatch.setattr(RetentionRepository, "keep_for", lambda _: 1)
    monkeypatch.setattr(ImageLineRepository, "latest", lambda _: None)
    monkeypatch.setattr(ImageLineRepository, "retention_labels", lambda *_: {})
    monkeypatch.setattr(
        ActivityRepository,
        "records",
        lambda _: {
            "operations": [],
            "capture_requests": [],
            "recreate_requests": [],
            "deletions": [],
        },
    )

    with django_assert_num_queries(0):
        detail = InventoryInspectionService.inspect(runner)

    images = {row["id"]: row for row in detail["generations"]}
    assert images[str(built.id)]["definition_name"] == "Build line"
    assert images[str(captured.id)]["line_name"] == "Captured line"
    for image in images.values():
        assert image["dependencies"][0]["id"] == str(workspace.id)
        assert image["dependencies"][0]["allocated_bytes"] == 123
