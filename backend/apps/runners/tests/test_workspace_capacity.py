"""Capacity is reserved by transactions, including during delayed VM results."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import connections

from apps.runners.models import CaptureRequest, LifecycleCommand, Task, Workspace
from apps.runners.operations import OperationRepository
from apps.runners.repositories import TaskRepository, WorkspaceRepository
from apps.runners.services.capture import CaptureService
from apps.runners.services.recreate import RecreateService
from apps.runners.services.workspace_capacity import WorkspaceCapacityService
from apps.runners.services.workspace_configuration import WorkspaceConfigurationService
from apps.runners.tests.conftest import has_busy_agent
from apps.runners.tests.test_image_deletion_coordinator import image
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db(transaction=True)


def create(runner, user, **resources):
    workspace_id = uuid.uuid4()
    return WorkspaceConfigurationService().create(
        workspace_fields={
            "workspace_id": workspace_id,
            "runner": runner,
            "name": "Capacity admission",
            "created_by": user,
            **resources,
        },
        runner=runner,
        user=user,
        organization_id=runner.organization_id,
        credentials=[],
        plugin_ids=[],
        task_id=uuid.uuid4(),
        operation_payload={"workspace_id": str(workspace_id)},
    )


def race(left, right):
    """Use independent real DB transactions on SQLite and PostgreSQL."""
    barrier = Barrier(2)

    def attempt(action):
        connections.close_all()
        try:
            barrier.wait(timeout=10)
            try:
                action()
                return True
            except ConflictError:
                return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, action) for action in (left, right)]
        return [future.result(timeout=30) for future in futures]


@pytest.mark.parametrize("dimension", ["vcpus", "memory_mb", "disk_size_gb"])
@pytest.mark.parametrize("operation", ["create", "resume", "update", "mixed"])
def test_parallel_admission_reserves_capacity(runner, user, dimension, operation):
    """Only one contender can consume the final available resource slot."""
    default = getattr(runner, f"qemu_default_{dimension}")
    setattr(runner, f"qemu_max_active_{dimension}", default)
    runner.save()
    before = 0
    if operation == "create":
        actions = [lambda: create(runner, user)] * 2
    elif operation in {"resume", "mixed"}:
        stopped = [
            Workspace.objects.create(runner=runner, created_by=user, status="stopped")
            for _ in range(2 if operation == "resume" else 1)
        ]
        before = len(stopped)
        actions = [
            lambda ws=ws: WorkspaceCapacityService.resume(ws.id, uuid.uuid4())
            for ws in stopped
        ]
        if operation == "mixed":
            actions.append(lambda: create(runner, user))
    else:
        setattr(runner, f"qemu_max_active_{dimension}", default * 3)
        runner.save()
        running = [
            Workspace.objects.create(runner=runner, created_by=user, status="running")
            for _ in range(2)
        ]
        before = 2
        values = [
            runner.qemu_default_vcpus,
            runner.qemu_default_memory_mb,
            runner.qemu_default_disk_size_gb,
        ]
        values[["vcpus", "memory_mb", "disk_size_gb"].index(dimension)] *= 2
        actions = [
            lambda ws=ws: WorkspaceConfigurationService().update(
                workspace_id=ws.id,
                user=user,
                organization_id=runner.organization_id,
                credentials=None,
                plugin_ids=None,
                qemu_values=tuple(values),
            )
            for ws in running
        ]

    # Both stopped disks already exceed a subsequently lowered allocated-disk
    # limit. Neither resume can create a valid reservation until disk is freed.
    overallocated = operation == "resume" and dimension == "disk_size_gb"
    assert sorted(race(*actions)) == (
        [False, False] if overallocated else [False, True]
    )
    assert Task.objects.filter(status="pending").count() == (0 if overallocated else 1)
    assert len(WorkspaceRepository.list_reserved_qemu_by_runner(runner.id)) == (
        0 if overallocated else (2 if operation == "update" else 1)
    )
    assert (
        Workspace.objects.count()
        == before + Workspace.objects.filter(status="creating").count()
    )


def test_unknown_create_outcome_keeps_capacity_until_resolved(runner, user):
    runner.qemu_max_active_vcpus = runner.qemu_default_vcpus
    runner.save()
    workspace, task = create(runner, user)
    OperationRepository.intervene(task, "Runner vanished; intervention required")
    workspace.refresh_from_db()
    assert workspace.status == "failed" and workspace.current_task_id == task.id
    with pytest.raises(ConflictError, match="vCPU limit exceeded"):
        create(runner, user)
    assert Workspace.objects.count() == 1
    from unittest.mock import Mock

    from apps.runners.services import RunnerService
    from apps.runners.services.disposition import DispositionService
    from apps.runners.tests.test_automatic_recovery_matrix import evidence

    assert DispositionService.recover_inspection(
        RunnerService(frontend_bus=Mock()),
        str(runner.id),
        runner.sid,
        evidence(LifecycleCommand.objects.get(task=task), "absent"),
    )
    create(runner, user)
    assert Workspace.objects.count() == 2


def test_capture_keeps_capacity_between_child_tasks(runner, workspace):
    runner.qemu_max_active_vcpus = runner.qemu_default_vcpus
    runner.save()
    _, child = CaptureService.allocate(
        workspace.id, "Capture reservation", has_busy_agent=has_busy_agent
    )
    workspace.status = "stopped"
    workspace.save(update_fields=["status"])
    TaskRepository.complete(child)
    workspace.refresh_from_db()
    assert workspace.current_task_id is None
    assert CaptureRequest.objects.get(workspace=workspace).phase == "stop"
    with pytest.raises(ConflictError, match="vCPU limit exceeded"):
        create(runner, workspace.created_by)


@pytest.mark.parametrize("workflow", ["capture", "recreate"])
def test_stopped_workflow_checks_capacity_before_any_destructive_child(
    runner, workspace, stopped_workspace, user, workflow
):
    runner.qemu_max_active_vcpus = runner.qemu_default_vcpus
    runner.save()
    base = image(runner, user)
    stopped_workspace.base_image_instance = base
    stopped_workspace.save()
    with pytest.raises(ConflictError, match="vCPU limit exceeded"):
        if workflow == "capture":
            CaptureService.allocate(
                stopped_workspace.id, "No capacity", has_busy_agent=has_busy_agent
            )
        else:
            RecreateService.allocate(
                stopped_workspace.id, base.id, has_busy_agent=has_busy_agent
            )
    assert not Task.objects.exists()
    assert not CaptureRequest.objects.exists()
    stopped_workspace.refresh_from_db()
    assert stopped_workspace.pending_base_image_instance_id is None
    assert stopped_workspace.current_task_id is None
    assert stopped_workspace.status == "stopped"


@pytest.mark.parametrize("outcome", ["pending", "failed", "intervention", "completed"])
def test_downsize_releases_capacity_only_after_successful_confirmation(
    runner, workspace, user, outcome
):
    """Saving a smaller desired configuration does not shrink a delayed VM."""
    runner.qemu_max_active_vcpus = 4
    runner.save()
    workspace.qemu_vcpus = 4
    workspace.save()
    updated = WorkspaceConfigurationService().update(
        workspace_id=workspace.id,
        user=user,
        organization_id=runner.organization_id,
        credentials=None,
        plugin_ids=None,
        qemu_values=(
            2,
            runner.qemu_default_memory_mb,
            runner.qemu_default_disk_size_gb,
        ),
    )
    task = updated._lifecycle_task
    command = LifecycleCommand.objects.get(task=task)
    assert command.payload["_previous_qemu_resources"]["vcpus"] == 4
    delivered = OperationRepository.delivery_payload(command)
    assert "_previous_qemu_resources" not in delivered
    if outcome == "failed":
        TaskRepository.fail(task, "Guest update failed before resizing")
    elif outcome == "intervention":
        OperationRepository.intervene(task, "Runner vanished; intervention required")
    elif outcome == "completed":
        TaskRepository.complete(task)
    if outcome == "completed":
        create(runner, user, qemu_vcpus=2)
        assert Workspace.objects.count() == 2
    else:
        with pytest.raises(ConflictError, match="vCPU limit exceeded"):
            create(runner, user, qemu_vcpus=2)
        assert Workspace.objects.count() == 1


def test_second_update_preserves_previous_failed_resource_floor(
    runner, workspace, user
):
    runner.qemu_max_active_vcpus = 8
    runner.save()
    workspace.qemu_vcpus = 8
    workspace.save()
    service = WorkspaceConfigurationService()
    for desired in [4, 2]:
        updated = service.update(
            workspace_id=workspace.id,
            user=user,
            organization_id=runner.organization_id,
            credentials=None,
            plugin_ids=None,
            qemu_values=(desired, 4096, 50),
        )
        command = LifecycleCommand.objects.get(task=updated._lifecycle_task)
        assert command.payload["_previous_qemu_resources"]["vcpus"] == 8
        TaskRepository.fail(updated._lifecycle_task, "Update failed")
    with pytest.raises(ConflictError, match="vCPU limit exceeded"):
        create(runner, user, qemu_vcpus=1)


@pytest.mark.parametrize("dimension", ["vcpus", "memory_mb", "disk_size_gb"])
def test_create_uses_current_runner_resource_limits_under_lock(runner, user, dimension):
    maximum = getattr(runner, f"qemu_max_{dimension}")
    stale = type(runner).objects.get(pk=runner.id)
    setattr(runner, f"qemu_max_{dimension}", maximum - 1)
    runner.save()
    with pytest.raises(ConflictError, match="value must be between"):
        create(stale, user, **{f"qemu_{dimension}": maximum})
    assert not Workspace.objects.exists()
    assert not Task.objects.exists()


@pytest.mark.parametrize("observed", ["running", "stopped", "unknown", "absent"])
@pytest.mark.parametrize("dimension", ["vcpus", "memory_mb", "disk_size_gb"])
def test_failed_guest_resource_reservation(runner, user, observed, dimension):
    """Workflow failure does not imply that its preserved VM bytes disappeared."""
    from unittest.mock import Mock

    from apps.runners.services import RunnerService
    from apps.runners.services.disposition import DispositionService
    from apps.runners.tests.test_automatic_recovery_matrix import evidence

    workspace, task = create(runner, user)
    command = LifecycleCommand.objects.get(task=task)
    assert DispositionService.recover_inspection(
        RunnerService(frontend_bus=Mock()),
        str(runner.id),
        runner.sid,
        evidence(command, observed),
    )
    workspace.refresh_from_db()
    assert workspace.status == "failed" and workspace.current_task_id is None
    setattr(
        runner,
        f"qemu_max_active_{dimension}",
        getattr(runner, f"qemu_default_{dimension}"),
    )
    runner.save()
    reserved = observed in {"running", "unknown"} or (
        observed == "stopped" and dimension == "disk_size_gb"
    )
    if not reserved:
        create(runner, user)
        return
    label = {"vcpus": "vCPU", "memory_mb": "RAM", "disk_size_gb": "disk"}[dimension]
    expected = f"{label} limit exceeded"
    with pytest.raises(ConflictError, match=expected):
        create(runner, user)
    assert Workspace.objects.count() == 1


def test_stopped_workspace_retains_disk_capacity(runner, stopped_workspace, user):
    runner.qemu_max_active_disk_size_gb = runner.qemu_default_disk_size_gb
    runner.save()
    with pytest.raises(ConflictError, match="disk limit exceeded"):
        create(runner, user)
    runner.qemu_max_active_disk_size_gb = None
    runner.qemu_max_active_vcpus = runner.qemu_default_vcpus
    runner.qemu_max_active_memory_mb = runner.qemu_default_memory_mb
    runner.save()
    create(runner, user)


def test_unissued_failure_releases_all_capacity(runner, user):
    workspace, task = create(runner, user)
    OperationRepository.fail_unissued(task, "Local payload preparation failed")
    for dimension in ("vcpus", "memory_mb", "disk_size_gb"):
        setattr(
            runner,
            f"qemu_max_active_{dimension}",
            getattr(runner, f"qemu_default_{dimension}"),
        )
    runner.save()
    create(runner, user)
    workspace.refresh_from_db()
    assert workspace.current_task_id is None


def test_deleted_workspace_releases_retained_capacity(runner, user):
    workspace, task = create(runner, user)
    OperationRepository.fail_unissued(task, "Local preparation failed")
    LifecycleCommand.objects.filter(task=task).update(
        payload={"_observed_workspace_status": "running"}
    )
    Workspace.objects.filter(pk=workspace.id).update(status="deleted")
    for dimension in ("vcpus", "memory_mb", "disk_size_gb"):
        setattr(
            runner,
            f"qemu_max_active_{dimension}",
            getattr(runner, f"qemu_default_{dimension}"),
        )
    runner.save()
    create(runner, user)


def test_delete_failed_retains_resource_capacity(runner, workspace, user):
    Workspace.objects.filter(pk=workspace.id).update(status="delete_failed")
    runner.qemu_max_active_vcpus = runner.qemu_default_vcpus
    runner.save()
    with pytest.raises(ConflictError, match="vCPU limit exceeded"):
        create(runner, user)


def test_resource_observation_is_backend_only(runner, workspace):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
        operation_payload={"_observed_workspace_status": "stopped"},
    )
    command = LifecycleCommand.objects.get(task=task)
    assert "_observed_workspace_status" not in OperationRepository.delivery_payload(
        command
    )


def test_new_attempt_does_not_inherit_absence_proof(runner, workspace, user):
    first = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    OperationRepository.fail_unissued(first, "Never delivered")
    second = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="resume_workspace",
    )
    TaskRepository.fail(second, "Known attempt failed without runtime observation")
    Workspace.objects.filter(pk=workspace.id).update(status="failed")
    runner.qemu_max_active_vcpus = runner.qemu_default_vcpus
    runner.save()
    with pytest.raises(ConflictError, match="vCPU limit exceeded"):
        create(runner, user)


@pytest.mark.parametrize(
    "kind,event,details,expected",
    [
        ("create_workspace", "workspace:created", {"status": "created"}, "running"),
        ("resume_workspace", "workspace:resumed", {}, "running"),
        ("update_workspace", "workspace:updated", {}, "running"),
        ("stop_workspace", "workspace:stopped", {}, "stopped"),
        ("remove_workspace", "workspace:removed", {"result": "deleted"}, "absent"),
        ("remove_workspace", "workspace:removed", {"result": "unconfirmed"}, "unknown"),
        (
            "stop_workspace",
            "workspace:error",
            {
                "error": "Credential scrub failed after stopping VM",
                "observed_status": "stopped",
                "outcome_known": True,
            },
            "stopped",
        ),
    ],
)
def test_terminal_result_records_runtime_evidence(
    runner, workspace, user, kind, event, details, expected
):
    from unittest.mock import Mock

    from apps.runners.services import RunnerService
    from apps.runners.services.operation_results import apply_result

    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type=kind,
    )
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(
        command,
        {
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
            "execution_finished": True,
            **details,
        },
    )
    assert apply_result(
        RunnerService(frontend_bus=Mock()),
        str(runner.id),
        event,
        data,
    )
    command.refresh_from_db()
    assert command.payload["_observed_workspace_status"] == expected


@pytest.mark.parametrize("dimension", ["vcpus", "memory_mb", "disk_size_gb"])
def test_stopped_settings_reserve_disk_without_compute(
    runner, workspace, stopped_workspace, user, dimension
):
    for name in ("vcpus", "memory_mb", "disk_size_gb"):
        setattr(
            runner, f"qemu_max_active_{name}", getattr(runner, f"qemu_default_{name}")
        )
    runner.qemu_max_active_disk_size_gb *= 2
    runner.save()
    values = [
        runner.qemu_default_vcpus,
        runner.qemu_default_memory_mb,
        runner.qemu_default_disk_size_gb,
    ]
    values[["vcpus", "memory_mb", "disk_size_gb"].index(dimension)] *= 2
    kwargs = {
        "workspace_id": stopped_workspace.id,
        "user": user,
        "organization_id": runner.organization_id,
        "credentials": None,
        "plugin_ids": None,
        "qemu_values": tuple(values),
    }
    if dimension == "disk_size_gb":
        with pytest.raises(ConflictError, match="disk limit exceeded"):
            WorkspaceConfigurationService().update(**kwargs)
        stopped_workspace.refresh_from_db()
        assert stopped_workspace.qemu_disk_size_gb is None
    else:
        updated = WorkspaceConfigurationService().update(**kwargs)
        assert (
            getattr(updated, f"qemu_{dimension}")
            == values[["vcpus", "memory_mb", "disk_size_gb"].index(dimension)]
        )
    assert not Task.objects.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "stop_workspace",
        "resume_workspace",
        "update_workspace",
        "remove_workspace",
        "create_image_artifact",
        "create_workspace",
        "create_workspace_from_image_artifact",
    ],
)
@pytest.mark.parametrize("status", ["running", "stopped"])
def test_unissued_existing_guest_never_claims_absence(runner, workspace, kind, status):
    Workspace.objects.filter(pk=workspace.id).update(status=status)
    workspace.refresh_from_db()
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type=kind,
        operation_payload={},
    )
    OperationRepository.fail_unissued(task, "Payload preparation failed locally")
    command = LifecycleCommand.objects.get(task=task)
    assert command.payload.get("_observed_workspace_status") != "absent"
    reserved = WorkspaceRepository.list_reserved_qemu_by_runner(
        runner.id, include_retained_disk=True
    )
    assert {row.id for row in reserved} == {workspace.id}
    workspace.refresh_from_db()
    assert workspace.current_task_id is None


@pytest.mark.parametrize("status", ["running", "stopped"])
def test_disk_settings_cannot_shrink_retained_volume(runner, workspace, user, status):
    Workspace.objects.filter(pk=workspace.id).update(
        status=status, qemu_disk_size_gb=50
    )
    with pytest.raises(ConflictError, match="cannot be reduced"):
        WorkspaceConfigurationService().update(
            workspace_id=workspace.id,
            user=user,
            organization_id=runner.organization_id,
            credentials=None,
            plugin_ids=None,
            qemu_values=(2, 4096, 20),
        )
    workspace.refresh_from_db()
    assert workspace.qemu_disk_size_gb == 50
    assert not Task.objects.exists()


@pytest.mark.parametrize("status", ["running", "stopped"])
def test_confirmed_disk_floor_survives_legacy_desired_shrink(
    runner, workspace, user, status
):
    workspace.qemu_disk_size_gb = 50
    workspace.save()
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="resume_workspace",
    )
    TaskRepository.complete(task)
    Workspace.objects.filter(pk=workspace.id).update(
        status=status, qemu_disk_size_gb=20
    )
    runner.qemu_max_active_disk_size_gb = 50
    runner.save()
    with pytest.raises(ConflictError, match="disk limit exceeded"):
        create(runner, user, qemu_disk_size_gb=30)
    assert Workspace.objects.count() == 1


def test_unissued_remove_is_failed_and_retryable(runner, workspace):
    Workspace.objects.filter(pk=workspace.id).update(status="deleting")
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="remove_workspace",
    )
    OperationRepository.fail_unissued(task, "Preparation failed before delivery")
    workspace.refresh_from_db()
    assert workspace.status == "delete_failed" and workspace.current_task_id is None
    retry = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="remove_workspace",
    )
    assert retry.id != task.id


@pytest.mark.parametrize("dimension", ["vcpus", "memory_mb", "disk_size_gb"])
def test_legacy_null_resources_keep_confirmed_sizes_on_resume(
    runner, workspace, user, dimension
):
    confirmed = getattr(runner, f"qemu_default_{dimension}") * 2
    setattr(workspace, f"qemu_{dimension}", confirmed)
    workspace.save()
    original = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="resume_workspace",
    )
    TaskRepository.complete(original)
    Workspace.objects.filter(pk=workspace.id).update(
        **{f"qemu_{dimension}": None, "status": "running"}
    )
    setattr(runner, f"qemu_max_active_{dimension}", confirmed)
    runner.save()
    with pytest.raises(ConflictError, match="limit exceeded"):
        create(runner, user)
    from unittest.mock import AsyncMock, Mock

    from asgiref.sync import async_to_sync

    from apps.runners.services import RunnerService

    default = getattr(runner, f"qemu_default_{dimension}")
    setattr(runner, f"qemu_default_{dimension}", default * 4)
    setattr(runner, f"qemu_max_active_{dimension}", default * 6)
    runner.save()
    Workspace.objects.create(
        runner=runner,
        created_by=user,
        status="running",
        **{f"qemu_{dimension}": default * 4},
    )
    Workspace.objects.filter(pk=workspace.id).update(status="stopped")
    task = async_to_sync(
        RunnerService(sio_server=AsyncMock(), frontend_bus=Mock()).resume_workspace
    )(workspace.id)
    workspace.refresh_from_db()
    assert getattr(workspace, f"qemu_{dimension}") == confirmed
    payload = LifecycleCommand.objects.get(task=task).payload
    assert payload[f"qemu_{dimension}"] == confirmed
    assert all(
        type(payload[f"qemu_{name}"]) is int
        for name in ("vcpus", "memory_mb", "disk_size_gb")
    )
