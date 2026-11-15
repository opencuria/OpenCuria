"""Virtual image size must match workspace sizing, reservations and commands."""

from __future__ import annotations

import uuid

import pytest
from django.db import transaction

from apps.runners.inventory_repository import InventoryRepository
from apps.runners.models import (
    CaptureRequest,
    ImageDefinition,
    ImageInstance,
    LifecycleCommand,
    Task,
    WorkspaceRecreateRequest,
)
from apps.runners.operations import OperationRepository
from apps.runners.repositories import ImageGenerationRepository as Generations
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService
from apps.runners.services.capture import CaptureService
from apps.runners.services.workspace_capacity import WorkspaceCapacityService
from apps.runners.services.workspace_configuration import WorkspaceConfigurationService
from apps.runners.tests.conftest import has_busy_agent
from apps.runners.tests.test_inventory_persistence import scan
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db(transaction=True)
GIB = 1 << 30


def build(runner, user, minimum=None):
    definition = ImageDefinition.objects.create(
        name="Disk size", organization=runner.organization
    )
    job, image, task = Generations.request(
        definition=definition, runner=runner, created_by=user, rendered_input={}
    )
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(command, command.payload)
    data.update(image_path=image.runner_ref, min_disk_size_gb=minimum)
    assert RunnerService().apply_operation_result(str(runner.id), "image:built", data)
    image.refresh_from_db()
    return image


def create(runner, user, image, requested=20):
    workspace_id = uuid.uuid4()
    return WorkspaceConfigurationService().create(
        workspace_fields={
            "workspace_id": workspace_id,
            "runner": runner,
            "name": "Sized guest",
            "created_by": user,
            "base_image_instance": image,
            "qemu_disk_size_gb": requested,
        },
        runner=runner,
        user=user,
        organization_id=runner.organization_id,
        credentials=[],
        plugin_ids=[],
        task_id=uuid.uuid4(),
        operation_payload={
            "workspace_id": str(workspace_id),
            "qemu_disk_size_gb": requested,
        },
    )


def report(runner, image, *, virtual_bytes=50 * GIB, complete=True):
    return InventoryRepository.record(
        str(runner.id),
        runner.sid,
        scan(
            complete=complete,
            resources=[
                {
                    "resource_id": image.runner_ref,
                    "kind": "image",
                    "managed": True,
                    "state": "ready",
                    "virtual_bytes": virtual_bytes,
                    "allocated_bytes": GIB,
                    "metadata": {
                        "artifact_id": str(image.id),
                        "operation_id": str(image.creating_task_id),
                    },
                }
            ],
        ),
    )


def test_built_disk_bound_controls_creation_and_resume(runner, user):
    image = build(runner, user, minimum=50)
    assert image.min_disk_size_gb == 50
    workspace, task = create(runner, user, image)
    assert workspace.qemu_disk_size_gb == 50
    assert LifecycleCommand.objects.get(task=task).payload["qemu_disk_size_gb"] == 50
    TaskRepository.complete(task)
    workspace.status = "stopped"
    workspace.save(update_fields=["status"])
    _, resumed = WorkspaceCapacityService.resume(workspace.id, uuid.uuid4())
    assert LifecycleCommand.objects.get(task=resumed).payload["qemu_disk_size_gb"] == 50


def test_actual_image_bound_is_reserved_before_creation(runner, user):
    image = build(runner, user, minimum=50)
    runner.qemu_max_active_disk_size_gb = 40
    runner.save()
    with pytest.raises(ConflictError, match="disk limit exceeded"):
        create(runner, user, image, requested=20)
    assert not Task.objects.filter(workspace__isnull=False).exists()


def test_inventory_persists_virtual_bound_rounded_up(runner, user):
    image = build(runner, user)
    assert report(runner, image, virtual_bytes=50 * GIB + 1)
    image.refresh_from_db()
    assert image.min_disk_size_gb == 51
    assert image.size_bytes == GIB  # Physical allocation is a separate size.


@pytest.mark.parametrize("old_bound", [None, 20])
def test_old_complete_inventory_bound_is_refreshed_inside_selection(
    runner, user, old_bound
):
    image = build(runner, user)
    assert report(runner, image)
    # Simulate an inventory row recorded before disk bounds were persisted.
    ImageInstance.objects.filter(pk=image.id).update(min_disk_size_gb=old_bound)
    image.refresh_from_db()
    assert image.min_disk_size_gb == old_bound
    workspace, task = create(runner, user, image, requested=20)
    assert workspace.qemu_disk_size_gb == 50
    assert LifecycleCommand.objects.get(task=task).payload["qemu_disk_size_gb"] == 50
    image.refresh_from_db()
    assert image.min_disk_size_gb == 50


def test_partial_inventory_cannot_invent_a_disk_bound(runner, user):
    image = build(runner, user)
    assert report(runner, image, complete=False)
    with transaction.atomic():
        selected = Generations.validate_selection(image.id)
    assert selected.min_disk_size_gb is None


@pytest.mark.parametrize("minimum", [0, -1, "50", True])
def test_invalid_build_disk_bound_is_not_published(runner, user, minimum):
    definition = ImageDefinition.objects.create(
        name="Invalid disk", organization=runner.organization
    )
    job, image, task = Generations.request(
        definition=definition, runner=runner, created_by=user, rendered_input={}
    )
    assert not Generations.finish(
        task_id=task.id,
        build_job_id=job.id,
        runner_id=str(runner.id),
        runner_ref=image.runner_ref,
        min_disk_size_gb=minimum,
    )
    image.refresh_from_db()
    assert image.status == "building" and image.min_disk_size_gb is None


@pytest.mark.parametrize("capacity", [40, 100])
@pytest.mark.parametrize("prior_running", [False, True])
def test_capture_corrects_source_disk_before_replacement_admission(
    runner, stopped_workspace, capacity, prior_running
):
    stopped_workspace.qemu_disk_size_gb = 20
    stopped_workspace.save()
    runner.qemu_max_active_disk_size_gb = capacity
    runner.save()
    _, task = CaptureService.allocate(
        stopped_workspace.id, "Actual disk", has_busy_agent=has_busy_agent
    )
    CaptureRequest.objects.filter(workspace=stopped_workspace).update(
        prior_running=prior_running
    )
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(command, command.payload)
    data.update(
        image_artifact_id="/images/actual.qcow2",
        name="Actual disk",
        min_disk_size_gb=50,
    )
    assert RunnerService().apply_operation_result(
        str(runner.id), "image_artifact:created", data
    )
    stopped_workspace.refresh_from_db()
    assert stopped_workspace.qemu_disk_size_gb == 50
    request = CaptureRequest.objects.get(workspace=stopped_workspace)
    request.image.refresh_from_db()
    assert request.image.min_disk_size_gb == 50
    outcome = CaptureService.advance(request.id)
    if capacity == 40:
        assert "cannot reserve runner resources" in outcome["diagnostic"]
        assert not WorkspaceRecreateRequest.objects.exists()
        assert not Task.objects.filter(type="remove_workspace").exists()
        assert not Task.objects.filter(type="resume_workspace").exists()
    else:
        assert outcome is None
        recreated = WorkspaceRecreateRequest.objects.get(workspace=stopped_workspace)
        assert recreated.child.type == "remove_workspace"
