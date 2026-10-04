"""Physical inventory is authenticated evidence, not inferred DB state."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.runners.inventory_repository import InventoryRepository
from apps.runners.models import InventoryEdge, InventorySnapshot

pytestmark = pytest.mark.django_db


def scan(sequence=1, complete=True, epoch=None, resources=None):
    return {
        "schema_version": 1,
        "inventory_epoch": epoch or str(uuid.uuid4()),
        "inventory_sequence": sequence,
        "complete": complete,
        "runtimes": [
            {
                "runtime_type": runtime,
                "collected_at": timezone.now().isoformat(),
                "complete": complete,
                "errors": [] if complete else ["inspection failed"],
                "resources": resources or [],
            }
            for runtime in ["docker", "qemu"]
        ],
    }


def test_sequence_session_and_partial_retention(runner):
    payload = scan(
        resources=[
            {
                "resource_id": "image",
                "kind": "image",
                "managed": True,
                "dependencies": ["unknown-base"],
            }
        ]
    )
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    assert not InventoryRepository.record(str(runner.id), runner.sid, payload)
    assert not InventoryRepository.record(str(runner.id), "old-sid", scan())
    assert not InventoryRepository.record(str(runner.id), runner.sid, scan())
    assert InventoryEdge.objects.filter(target__kind="unknown").count() == 2
    partial = scan(2, False, payload["inventory_epoch"])
    assert InventoryRepository.record(str(runner.id), runner.sid, partial)
    detail = InventoryRepository.detail(runner)
    assert not detail["latest_complete"]
    assert not detail["runtimes"][0]["fresh"]
    assert len(detail["runtimes"][0]["resources"]) == 2
    assert detail["runtimes"][0]["resources"][0]["logical_bytes"] is None
    assert detail["runtimes"][0]["diagnostics"]["errors"] == ["inspection failed"]
    InventorySnapshot.objects.update(received_at=timezone.now() - timedelta(minutes=6))
    assert not InventoryRepository.detail(runner)["runtimes"][0]["fresh"]


def test_malformed_snapshot_transaction_rolls_back(runner):
    payload = scan(
        resources=[
            {
                "resource_id": "bad",
                "kind": "image",
                "managed": True,
                "logical_bytes": -1,
            }
        ]
    )
    assert not InventoryRepository.record(str(runner.id), runner.sid, payload)
    assert InventorySnapshot.objects.count() == 0


def test_workspace_join_uses_actual_metadata(runner, workspace):
    payload = scan(
        resources=[
            {
                "resource_id": "domain",
                "kind": "workspace",
                "managed": True,
                "metadata": {"workspace_id": str(workspace.id)},
            }
        ]
    )
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    observed = InventoryRepository.detail(runner)["runtimes"][0]["resources"][0]
    assert observed["workspace"]["id"] == str(workspace.id)
    assert observed["workspace"]["status"] == "running"


def test_storage_admin_authorization(runner, user, organization):
    from apps.organizations.models import Membership
    from apps.runners.services.storage import StorageService
    from common.exceptions import AuthenticationError, NotFoundError

    service = StorageService()
    with pytest.raises(NotFoundError):
        service.detail(user, organization.id, runner.id)
    Membership.objects.create(user=user, organization=organization, role="admin")
    membership = Membership.objects.get(user=user, organization=organization)
    membership.role = "member"
    membership.save()
    with pytest.raises(AuthenticationError):
        service.detail(user, organization.id, runner.id)
    membership.role = "admin"
    membership.save()
    assert service.detail(user, organization.id, runner.id)["runner_id"] == str(
        runner.id
    )
    with pytest.raises(NotFoundError):
        service.detail(user, organization.id, uuid.uuid4())


def test_refresh_offline_durable_and_partial_does_not_fulfill(offline_runner):
    from apps.runners.models import InventoryRefresh

    InventoryRepository.request_refresh(offline_runner)
    assert InventoryRepository.refresh_candidates() == []
    offline_runner.status = "online"
    offline_runner.sid = "new-session"
    offline_runner.save()
    assert len(InventoryRepository.refresh_candidates()) == 1
    partial = scan(complete=False)
    assert InventoryRepository.record(str(offline_runner.id), "new-session", partial)
    assert InventoryRefresh.objects.get(runner=offline_runner).fulfilled_at is None
    complete = scan(2, True, partial["inventory_epoch"])
    assert InventoryRepository.record(str(offline_runner.id), "new-session", complete)
    assert InventoryRefresh.objects.get(runner=offline_runner).fulfilled_at


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_worker_tick_delivers_refresh_without_http(runner):
    from unittest.mock import AsyncMock

    from apps.runners.services.recovery import RecoveryService

    InventoryRepository.request_refresh(runner)
    transport = AsyncMock()
    await RecoveryService().tick(transport)
    transport.emit.assert_awaited_once_with("inventory:refresh", {}, room=runner.sid)


def test_foreign_runner_hidden_from_admin(runner, user, organization):
    from apps.organizations.models import Membership, Organization
    from apps.runners.services.storage import StorageService
    from common.exceptions import NotFoundError

    other = Organization.objects.create(name="Other", slug="other-storage-test")
    Membership.objects.create(user=user, organization=other, role="admin")
    with pytest.raises(NotFoundError):
        StorageService().detail(user, other.id, runner.id)


def test_backfill_only_unique_physical_dependency_not_existing_conflicting_pin(
    runner, workspace, user
):
    from apps.runners.models import ImageInstance
    from apps.runners.tests.test_image_deletion_coordinator import observe

    workspace.runtime_type = "qemu"
    workspace.save()
    first = ImageInstance.objects.create(
        runner=runner,
        created_by=user,
        runtime_type="qemu",
        name="base",
        runner_ref="/images/actual",
        is_legacy=True,
    )
    second = ImageInstance.objects.create(
        runner=runner,
        created_by=user,
        runtime_type="qemu",
        name="other",
        runner_ref="/images/other",
        is_legacy=True,
    )
    resources = [
        {
            "resource_id": first.runner_ref,
            "kind": "image",
            "managed": True,
            "state": "ready",
        },
        {
            "resource_id": second.runner_ref,
            "kind": "image",
            "managed": True,
            "state": "ready",
        },
        {
            "resource_id": "domain",
            "kind": "workspace",
            "managed": True,
            "state": "running",
            "metadata": {"workspace_id": str(workspace.id)},
            "dependencies": [first.runner_ref, second.runner_ref],
        },
    ]
    observe(runner, resources)
    workspace.refresh_from_db()
    assert workspace.base_image_instance_id is None
    resources[-1]["dependencies"] = [first.runner_ref]
    observe(runner, resources)
    workspace.refresh_from_db()
    assert workspace.base_image_instance_id == first.id
    resources[-1]["dependencies"] = [second.runner_ref]
    observe(runner, resources)
    workspace.refresh_from_db()
    assert workspace.base_image_instance_id == first.id


def test_enriched_generation_db_pin_without_physical_observation(runner, workspace):
    from apps.runners.models import ImageInstance

    image = ImageInstance.objects.create(
        runner=runner,
        name="Historical base",
        runtime_type="qemu",
        origin_type="workspace_capture",
        origin_workspace=workspace,
        created_by=workspace.created_by,
        is_legacy=True,
        runner_ref="/cache/old.qcow2",
    )
    workspace.base_image_instance = image
    workspace.save(update_fields=["base_image_instance"])
    detail = InventoryRepository.detail(runner)
    generation = detail["generations"][0]
    assert generation["name"] == "Historical base"
    assert generation["observed_state"] == "unknown"
    assert generation["size_bytes"] is None
    assert generation["dependencies"][0]["name"] == workspace.name
    assert (
        generation["dependencies"][0]["last_activity_at"] == workspace.last_activity_at
    )
    assert "@" not in generation["dependencies"][0]["owner_label"]
    assert InventoryRepository.record(str(runner.id), runner.sid, scan())
    assert (
        InventoryRepository.detail(runner)["generations"][0]["observed_state"]
        == "missing"
    )
    runner.status = "offline"
    runner.save(update_fields=["status"])
    detail = InventoryRepository.detail(runner)
    assert not detail["runner_online"]
    assert detail["generations"][0]["observed_state"] == "unknown"


def test_recursive_physical_consumers_and_logical_pins_ignore_provenance(
    runner, workspace
):
    from apps.runners.models import ImageInstance
    from apps.runners.storage_schemas import RunnerStorageOut

    root = ImageInstance.objects.create(
        runner=runner,
        name="Root",
        runtime_type="qemu",
        is_legacy=True,
        origin_type="workspace_capture",
        runner_ref="root",
    )
    child = ImageInstance.objects.create(
        runner=runner,
        name="Child",
        runtime_type="qemu",
        is_legacy=True,
        origin_type="workspace_capture",
        runner_ref="child",
    )
    unrelated = ImageInstance.objects.create(
        runner=runner,
        name="Independent capture",
        runtime_type="qemu",
        is_legacy=True,
        origin_type="workspace_capture",
        runner_ref="independent",
        origin_workspace=workspace,
    )
    workspace.base_image_instance = child
    workspace.save(update_fields=["base_image_instance"])
    payload = scan(
        resources=[
            {"resource_id": "root", "kind": "image", "managed": True},
            {
                "resource_id": "child",
                "kind": "image",
                "managed": True,
                "dependencies": ["root"],
            },
            {
                "resource_id": "independent",
                "kind": "image",
                "managed": True,
                "provenance": "root",
            },
        ]
    )
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    detail = InventoryRepository.detail(runner)
    RunnerStorageOut.model_validate(detail)
    images = {i["id"]: i for i in detail["generations"]}
    assert images[str(root.id)]["dependencies"][0]["id"] == str(workspace.id)
    assert images[str(unrelated.id)]["dependencies"] == []


def test_docker_inherited_labels_do_not_join_generation(runner, user):
    from apps.runners.models import ImageInstance, InventoryResource

    generation = ImageInstance.objects.create(
        runner=runner,
        runtime_type="docker",
        name="final",
        created_by=user,
        runner_ref="placeholder",
        status="ready",
        is_legacy=True,
    )
    tag = f"opencuria/generations:{generation.id}"
    generation.runner_ref = tag
    generation.save()
    labels = {
        "opencuria.image-instance-id": str(generation.id),
        "opencuria.operation-id": "op",
    }
    resources = [
        {
            "resource_id": "intermediate",
            "kind": "build_cache",
            "managed": True,
            "logical_bytes": 900,
            "metadata": labels,
            "aliases": [],
        },
        {
            "resource_id": "final",
            "kind": "image",
            "managed": True,
            "logical_bytes": 100,
            "state": "observed",
            "metadata": labels,
            "aliases": [tag],
        },
    ]
    payload = scan(resources=resources)
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    assert InventoryResource.objects.filter(image=generation).count() == 1
    generation.refresh_from_db()
    assert generation.size_bytes == 100
    # Physical publication binding survives manual tag removal, labels alone do not.
    resources[1]["aliases"] = []
    resources[1]["metadata"] = {
        **labels,
        "published_image_id": "final",
        "expected_image_tag": tag,
    }
    payload = scan(2, epoch=payload["inventory_epoch"], resources=resources)
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    latest = InventorySnapshot.objects.latest("id")
    assert (
        InventoryResource.objects.filter(
            runtime__snapshot=latest, image=generation
        ).count()
        == 1
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("state", ["running", "stopped", "unknown"])
def test_workspace_state_authority_not_disk_order(runner, workspace, reverse, state):
    from apps.runners.models import ImageInstance
    from apps.runners.tests.test_image_deletion_coordinator import observe

    image = ImageInstance.objects.create(
        runner=runner,
        name="base",
        runtime_type="qemu",
        runner_ref="base",
        is_legacy=True,
    )
    workspace.runtime_type = "qemu"
    workspace.base_image_instance = image
    workspace.save()
    resources = [
        {
            "resource_id": "disk",
            "kind": "disk",
            "managed": True,
            "state": "observed",
            "metadata": {"workspace_id": str(workspace.id)},
            "dependencies": ["base"],
        },
        {
            "resource_id": "domain",
            "kind": "workspace",
            "managed": True,
            "state": state,
            "metadata": {"workspace_id": str(workspace.id)},
            "dependencies": ["disk"],
        },
        {"resource_id": "base", "kind": "image", "managed": True, "state": "ready"},
    ]
    observe(runner, list(reversed(resources)) if reverse else resources)
    detail = InventoryRepository.detail(runner)
    assert detail["generations"][0]["dependencies"][0]["observed_state"] == state
    # Partial scans retain last confirmed state without claiming freshness.
    observe(runner, [], complete=False)
    detail = InventoryRepository.detail(runner)
    assert detail["generations"][0]["dependencies"][0]["observed_state"] == state
    assert not next(r for r in detail["runtimes"] if r["runtime_type"] == "qemu")[
        "fresh"
    ]
    # Disk-only ownership still links dependents but never supplies runtime state.
    observe(runner, [r for r in resources if r["kind"] != "workspace"])
    assert (
        InventoryRepository.detail(runner)["generations"][0]["dependencies"][0][
            "observed_state"
        ]
        == "unknown"
    )


@pytest.mark.parametrize(
    "assignment_status,image_status,expected",
    [
        ("active", "ready", True),
        ("active", "deleted", False),
        ("deleted", "ready", False),
        ("deactivated", "ready", False),
    ],
)
def test_current_pointer_is_not_default_after_retirement(
    runner, assignment_status, image_status, expected
):
    from apps.runners.models import ImageBuildJob, ImageDefinition, ImageInstance

    definition = ImageDefinition.objects.create(
        name="recipe", organization=runner.organization
    )
    job = ImageBuildJob.objects.create(
        runner=runner, image_definition=definition, status=assignment_status
    )
    image = ImageInstance.objects.create(
        runner=runner,
        name="base",
        runtime_type="qemu",
        runner_ref="base",
        is_legacy=True,
        build_job=job,
        status=image_status,
    )
    job.current_generation = image
    job.save()
    assert (
        InventoryRepository.detail(runner)["generations"][0]["is_current"] is expected
    )


@pytest.mark.parametrize("physical", ["/cache/base.qcow2", "/other/base.qcow2"])
def test_exact_legacy_disk_reference_is_association_not_publication(
    runner, workspace, physical
):
    from apps.runners.deletion_repository import DeletionRepository
    from apps.runners.models import ImageInstance, InventoryResource

    image = ImageInstance.objects.create(
        runner=runner,
        name="Legacy",
        runtime_type="qemu",
        is_legacy=True,
        runner_ref="/cache/base.qcow2",
        size_bytes=17,
    )
    workspace.runtime_type = "qemu"
    workspace.base_image_instance = image
    workspace.save()
    payload = scan(
        resources=[
            {
                "resource_id": physical,
                "aliases": [image.runner_ref],
                "kind": "disk",
                "managed": physical.startswith("/cache/"),
                "state": "unknown",
                "allocated_bytes": 99,
            },
            {
                "resource_id": "domain",
                "kind": "workspace",
                "managed": True,
                "state": "stopped",
                "metadata": {"workspace_id": str(workspace.id)},
                "dependencies": [physical],
            },
        ]
    )
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    assert (
        InventoryResource.objects.get(
            runtime__runtime_type="qemu", physical_id=physical
        ).image_id
        == image.id
    )
    generation = InventoryRepository.detail(runner)["generations"][0]
    assert generation["observed_state"] == "unknown"
    assert generation["dependencies"][0]["id"] == str(workspace.id)
    image.refresh_from_db()
    workspace.refresh_from_db()
    assert image.size_bytes == 17
    assert image.is_legacy
    assert workspace.base_image_instance_id == image.id
    graph = DeletionRepository.preview(runner.organization_id, "image", image.id)
    assert any(b.startswith("unknown_or_foreign:") for b in graph["blockers"])
    assert not any(b.startswith("missing_image:") for b in graph["blockers"])


def test_legacy_shared_physical_alias_is_unverified_not_missing(runner):
    from apps.runners.models import ImageInstance, InventoryResource

    image = ImageInstance.objects.create(
        runner=runner,
        name="Legacy",
        runtime_type="qemu",
        is_legacy=True,
        runner_ref="/base.qcow2",
    )
    payload = scan(
        resources=[
            {
                "resource_id": path,
                "aliases": [image.runner_ref],
                "kind": "disk",
                "managed": True,
                "state": "unknown",
            }
            for path in ["/one/base.qcow2", "/two/base.qcow2"]
        ]
    )
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    assert not InventoryResource.objects.filter(image=image).exists()
    assert (
        InventoryRepository.detail(runner)["generations"][0]["observed_state"]
        == "unknown"
    )


@pytest.mark.parametrize("operation_matches", [False, True])
def test_qemu_uuid_manifest_requires_artifact_and_operation(runner, operation_matches):
    from apps.runners.models import ImageInstance, Task

    task = Task.objects.create(runner=runner, type="build_image")
    image = ImageInstance.objects.create(
        runner=runner,
        name="New",
        runtime_type="qemu",
        runner_ref="/base.qcow2",
        creating_task=task,
    )
    payload = scan(
        resources=[
            {
                "resource_id": image.runner_ref,
                "kind": "image",
                "managed": True,
                "state": "ready",
                "metadata": {
                    "artifact_id": str(image.id),
                    "operation_id": str(task.id)
                    if operation_matches
                    else str(uuid.uuid4()),
                },
            }
        ]
    )
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)
    assert InventoryRepository.detail(runner)["generations"][0]["observed_state"] == (
        "ready" if operation_matches else "unknown"
    )


def test_shared_legacy_database_reference_cannot_guess_identity(runner):
    from apps.runners.models import ImageInstance, InventoryResource

    for name in ["one", "two"]:
        ImageInstance.objects.create(
            runner=runner,
            name=name,
            runtime_type="qemu",
            is_legacy=True,
            runner_ref="/base.qcow2",
        )
    assert InventoryRepository.record(
        str(runner.id),
        runner.sid,
        scan(
            resources=[
                {
                    "resource_id": "/base.qcow2",
                    "kind": "disk",
                    "managed": True,
                    "state": "unknown",
                }
            ]
        ),
    )
    assert not InventoryResource.objects.filter(image__isnull=False).exists()
    assert all(
        g["observed_state"] == "unknown"
        for g in InventoryRepository.detail(runner)["generations"]
    )


def test_unpublished_capture_without_reference_is_not_missing(runner):
    from apps.runners.models import ImageInstance

    ImageInstance.objects.create(
        runner=runner,
        name="Failed capture",
        runtime_type="qemu",
        status="failed",
        runner_ref="",
    )
    assert InventoryRepository.record(str(runner.id), runner.sid, scan())
    assert (
        InventoryRepository.detail(runner)["generations"][0]["observed_state"]
        == "not_published"
    )


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {"filesystem_id": "123", "file_identity": "123:456"},
        {"filesystem_id": 123, "file_identity": [456]},
    ],
)
def test_sanitized_storage_identity_serialization(runner, workspace, identity):
    from apps.runners.storage_schemas import RunnerStorageOut

    metadata = {
        "workspace_id": str(workspace.id),
        "private_recipe": "secret",
        **(identity or {}),
    }
    assert InventoryRepository.record(
        str(runner.id),
        runner.sid,
        scan(
            resources=[
                {
                    "resource_id": "disk",
                    "kind": "disk",
                    "managed": True,
                    "metadata": metadata,
                }
            ]
        ),
    )
    detail = InventoryRepository.detail(runner)
    serialized = RunnerStorageOut.model_validate(detail).model_dump(mode="json")
    for runtime in serialized["runtimes"]:
        observed = runtime["resources"][0]
        assert observed["filesystem_id"] == (
            "123" if identity and isinstance(identity["filesystem_id"], str) else None
        )
        assert observed["file_identity"] == (
            "123:456"
            if identity and isinstance(identity["file_identity"], str)
            else None
        )
        assert observed["workspace"]["base_image_instance_id"] is None
        assert "metadata" not in observed and "private_recipe" not in observed
        assert "secret" not in str(observed)
        assert observed["allocated_bytes"] is None
        # REST clients may still validate older payloads without these fields.
        from apps.runners.storage_schemas import StorageResourceOut

        old = dict(observed)
        old.pop("filesystem_id")
        old.pop("file_identity")
        old["workspace"] = dict(old["workspace"])
        old["workspace"].pop("base_image_instance_id")
        validated = StorageResourceOut.model_validate(old)
        assert validated.filesystem_id is None and validated.file_identity is None
        assert validated.workspace.base_image_instance_id is None


def test_exact_base_pin_serialized_without_physical_base(runner, workspace):
    from apps.runners.models import ImageInstance
    from apps.runners.storage_schemas import RunnerStorageOut

    image = ImageInstance.objects.create(
        runner=runner,
        name="Orphan base",
        runtime_type="qemu",
        runner_ref="",
    )
    workspace.base_image_instance = image
    workspace.save(update_fields=["base_image_instance"])
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
    serialized = RunnerStorageOut.model_validate(
        InventoryRepository.detail(runner)
    ).model_dump(mode="json")
    assert serialized["generations"][0]["dependencies"][0][
        "base_image_instance_id"
    ] == str(image.id)
    for runtime in serialized["runtimes"]:
        assert runtime["resources"][0]["workspace"]["base_image_instance_id"] == str(
            image.id
        )
