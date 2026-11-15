"""Publication corruption must disable selection without erasing backing evidence."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.runners.inventory_repository import InventoryRepository
from apps.runners.models import ImageInstance, InventoryResource, Task
from apps.runners.repositories import ImageGenerationRepository
from apps.runners.tests.conftest import capture_version_fields
from apps.runners.tests.test_inventory_persistence import scan
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


def image_at(runner, user, path="/images/exact.qcow2"):
    """Create one previously published task-bound captured image."""
    task = Task.objects.create(
        runner=runner, type="create_image_artifact", status="completed"
    )
    return ImageInstance.objects.create(
        runner=runner,
        name="Published",
        runner_ref=path,
        status="ready",
        creating_task=task,
        **capture_version_fields(runner, user),
    )


def corrupt_resource(image, **changes):
    """Retain readable physical bytes while publication metadata is invalid."""
    return {
        "resource_id": image.runner_ref,
        "kind": "disk",
        "managed": True,
        "state": "unknown",
        "metadata": {"diagnostic_code": "invalid_publication"},
        **changes,
    }


def test_invalid_publication_disables_exact_ready_image(runner, user):
    image = image_at(runner, user)
    assert InventoryRepository.record(
        str(runner.id),
        runner.sid,
        scan(resources=[corrupt_resource(image)]),
    )
    image.refresh_from_db()
    assert image.status == "failed"
    assert (
        InventoryResource.objects.get(physical_id=image.runner_ref).image_id == image.id
    )
    with pytest.raises(ConflictError, match="no longer ready"):
        ImageGenerationRepository.validate_selection(image.id)


@pytest.mark.parametrize(
    "unsafe", ["partial", "stale", "foreign", "unmanaged", "ambiguous", "wrong_session"]
)
def test_untrusted_publication_observation_cannot_mutate_image(runner, user, unsafe):
    image = image_at(runner, user)
    resource = corrupt_resource(image)
    payload = scan(resources=[resource])
    session = runner.sid
    if unsafe == "partial":
        payload["complete"] = payload["runtimes"][0]["complete"] = False
    elif unsafe == "stale":
        payload["runtimes"][0]["collected_at"] = (
            timezone.now() - timedelta(minutes=6)
        ).isoformat()
    elif unsafe == "foreign":
        resource["resource_id"] = "/images/foreign.qcow2"
    elif unsafe == "unmanaged":
        resource["managed"] = False
    elif unsafe == "ambiguous":
        image_at(runner, user)
    else:
        session = "obsolete-session"
    assert InventoryRepository.record(str(runner.id), session, payload) is (
        unsafe != "wrong_session"
    )
    image.refresh_from_db()
    assert image.status == "ready"


def test_corrupt_publication_preserves_workspace_dependencies(runner, workspace, user):
    from apps.runners.deletion_repository import DeletionRepository
    from apps.runners.models import InventoryEdge

    image = image_at(runner, user)
    workspace.base_image_instance = image
    workspace.save()
    payload = scan(
        resources=[
            corrupt_resource(image),
            {
                "resource_id": "domain",
                "kind": "workspace",
                "managed": True,
                "state": "stopped",
                "metadata": {"workspace_id": str(workspace.id)},
                "dependencies": [image.runner_ref],
            },
        ]
    )
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    workspace.refresh_from_db()
    assert workspace.base_image_instance_id == image.id
    assert (
        InventoryEdge.objects.get(source__physical_id="domain").target.image_id
        == image.id
    )
    preview = DeletionRepository.preview(runner.organization_id, "image", image.id)
    assert {row["id"] for row in preview["workspaces"]} == {str(workspace.id)}


def published_build(runner, user):
    """Build a real task-bound current generation without touching the runner FS."""
    from apps.runners.models import ImageDefinition
    from apps.runners.tests.test_image_generations import finish, request

    definition = ImageDefinition.objects.create(
        name="Publication projection", organization=runner.organization
    )
    job, image, task = request(definition, runner, user)
    assert finish(job, image, task)
    job.refresh_from_db()
    return definition, job, image, task


def test_corrupt_current_publication_fails_assignment_and_summary(runner, user):
    from apps.runners.repositories import ImageDefinitionRepository

    definition, job, image, task = published_build(runner, user)
    job.build_log = "Successful original build remains history"
    job.save(update_fields=["build_log"])
    assert InventoryRepository.record(
        str(runner.id), runner.sid, scan(resources=[corrupt_resource(image)])
    )
    job.refresh_from_db()
    image.refresh_from_db()
    task.refresh_from_db()
    assert image.status == job.status == "failed"
    assert job.current_generation_id == image.id and job.pending_generation_id is None
    assert job.build_task_id == task.id and task.status == "completed"
    assert job.build_log == "Successful original build remains history"
    for projected in [
        definition,
        ImageDefinitionRepository.list_by_org(runner.organization_id).get(
            pk=definition.id
        ),
    ]:
        summary = ImageDefinitionRepository.build_summary(projected)
        assert summary["failed"] == 1 and summary["active"] == 0


@pytest.mark.parametrize(
    "mode",
    [
        "newer_pending",
        "pending",
        "building",
        "pending_deletion",
        "deleting",
        "deleted",
        "delete_failed",
        "deactivated",
        "newer_current",
    ],
)
def test_corrupt_publication_preserves_newer_or_inactive_assignment(runner, user, mode):
    from apps.runners.tests.test_image_generations import finish, request

    definition, job, image, _ = published_build(runner, user)
    if mode in {"newer_pending", "pending", "building", "newer_current"}:
        job, newer, new_task = request(definition, runner, user)
        if mode == "newer_current":
            assert finish(job, newer, new_task)
    if mode not in {"newer_pending", "newer_current"}:
        job.status = mode
        job.save(update_fields=["status"])
    job.refresh_from_db()
    before = (
        job.status,
        job.current_generation_id,
        job.pending_generation_id,
        job.build_task_id,
    )
    assert InventoryRepository.record(
        str(runner.id), runner.sid, scan(resources=[corrupt_resource(image)])
    )
    job.refresh_from_db()
    image.refresh_from_db()
    assert image.status == "failed"
    assert (
        job.status,
        job.current_generation_id,
        job.pending_generation_id,
        job.build_task_id,
    ) == before


def test_corrupt_current_can_rebuild_without_stale_result_promotion(
    runner, workspace, user
):
    from apps.runners.tests.test_image_generations import finish, request

    definition, job, first, original = published_build(runner, user)
    workspace.base_image_instance = first
    workspace.save(update_fields=["base_image_instance"])
    packet = scan(resources=[corrupt_resource(first)])
    assert InventoryRepository.record(str(runner.id), runner.sid, packet)
    job.refresh_from_db()
    assert job.status == "failed"
    job, second, retry = request(definition, runner, user)
    assert job.status == "pending" and job.pending_generation_id == second.id
    assert job.current_generation_id == first.id and second.generation == 2
    assert not finish(job, first, original)
    assert InventoryRepository.record(
        str(runner.id),
        runner.sid,
        scan(2, epoch=packet["inventory_epoch"], resources=[corrupt_resource(first)]),
    )
    job.refresh_from_db()
    assert job.status == "pending" and job.pending_generation_id == second.id
    assert finish(job, second, retry)
    assert not finish(job, first, original, error="late old failure")
    job.refresh_from_db()
    first.refresh_from_db()
    second.refresh_from_db()
    workspace.refresh_from_db()
    assert job.status == "active" and job.current_generation_id == second.id
    assert job.pending_generation_id is None and second.status == "ready"
    assert first.status == "failed" and workspace.base_image_instance_id == first.id
    assert ImageGenerationRepository.validate_selection(second.id).id == second.id


@pytest.mark.parametrize("corrupt_before_retry", [True, False])
def test_failed_rebuild_with_corrupt_current_remains_retryable(
    runner, user, corrupt_before_retry
):
    from apps.runners.tests.test_image_generations import finish, request

    definition, job, first, original = published_build(runner, user)
    if corrupt_before_retry:
        assert InventoryRepository.record(
            str(runner.id), runner.sid, scan(resources=[corrupt_resource(first)])
        )
    job, second, retry = request(definition, runner, user)
    if not corrupt_before_retry:
        assert InventoryRepository.record(
            str(runner.id), runner.sid, scan(resources=[corrupt_resource(first)])
        )
    assert finish(job, second, retry, error="replacement build failed")
    job.refresh_from_db()
    assert job.status == "failed" and job.pending_generation_id is None
    assert job.current_generation_id == first.id
    job, third, retry_again = request(definition, runner, user)
    assert job.status == "pending" and job.pending_generation_id == third.id
    assert not finish(job, first, original)
    assert not finish(job, second, retry)
    assert finish(job, third, retry_again)
    job.refresh_from_db()
    assert job.status == "active" and job.current_generation_id == third.id
