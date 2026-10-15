"""Persistent workspace processes are captured as immutable image state."""

from __future__ import annotations

import copy
import uuid
from unittest.mock import AsyncMock

import pytest

from apps.runners.capture_repository import CaptureRepository
from apps.runners.enums import ProcessStatus, TaskType, WorkspaceStatus
from apps.runners.models import (
    CapturedImage,
    CaptureRequest,
    ImageInstance,
    LifecycleCommand,
    Task,
    Workspace,
    WorkspaceProcess,
)
from apps.runners.repositories import TaskRepository, WorkspaceProcessRepository
from apps.runners.services import RunnerService
from apps.runners.services.workspace_configuration import WorkspaceConfigurationService
from common.exceptions import ConflictError

PROCESS_DEFINITIONS = [
    {"name": "api", "command": "python -m server", "workdir": "/srv/api"},
    {"name": "worker", "command": "rq worker", "workdir": "/srv/worker"},
]


def _make_image(
    runner, user, *, source=None, process_definitions=None, runtime="docker"
):
    """Create a ready legacy image with an explicit process snapshot."""
    return ImageInstance.objects.create(
        is_legacy=True,
        runner=runner,
        runtime_type=runtime,
        origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
        origin_workspace=source,
        created_by=user,
        name="Process snapshot image",
        runner_ref="image-with-processes",
        status=ImageInstance.Status.READY,
        process_definitions=(
            list(PROCESS_DEFINITIONS)
            if process_definitions is None
            else process_definitions
        ),
    )


def _make_process(workspace, user, *, name, command, workdir, **extra):
    return WorkspaceProcess.objects.create(
        workspace=workspace,
        created_by=user,
        name=name,
        command=command,
        workdir=workdir,
        **extra,
    )


async def _create_from_image(service, method, image, runner, user, name):
    options = {
        "image_artifact_id": image.id,
        "name": name,
        "user": user,
        "organization_id": runner.organization_id,
        "credentials": [],
    }
    if method == "create_workspace":
        return await service.create_workspace(repos=[], **options)
    return await service.create_workspace_from_image_artifact(**options)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "source_status", [WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED]
)
def test_capture_snapshots_every_persistent_definition_without_runtime_state(
    runner, user, source_status
):
    """Capture records definitions before stopping, regardless of source status."""
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Capture source",
        runtime_type="qemu",
        status=source_status,
    )
    expected = {
        "api": {"name": "api", "command": "python -m server", "workdir": "/srv/api"},
        "db": {"name": "db", "command": "postgres -D /data", "workdir": "/"},
        "failed": {"name": "failed", "command": "exit 1", "workdir": "/tmp"},
        "worker": {"name": "worker", "command": "rq worker", "workdir": "/srv/worker"},
    }
    states = {
        "api": ProcessStatus.RUNNING,
        "db": ProcessStatus.EXITED,
        "failed": ProcessStatus.FAILED,
        "worker": ProcessStatus.KILLED,
    }
    for name, definition in expected.items():
        _make_process(
            workspace,
            user,
            **definition,
            status=states[name],
            pid=3210,
            log_path=f".opencuria/processes/{name}.log",
            run_count=7,
            exit_code=9,
            session_id=uuid.uuid4(),
        )
    _make_process(
        workspace,
        user,
        name="temporary helper",
        command="python helper.py",
        workdir="/tmp",
        kind="temp",
        session_id=uuid.uuid4(),
    )

    _, task = CaptureRepository.allocate(workspace.id, "Snapshot")

    image = ImageInstance.objects.get(origin_workspace=workspace)
    definitions = image.process_definitions
    assert {item["name"]: item for item in definitions} == expected
    assert all(set(item) == {"name", "command", "workdir"} for item in definitions)
    assert all("temporary helper" != item["name"] for item in definitions)
    assert task.type == (
        TaskType.STOP_WORKSPACE
        if source_status == WorkspaceStatus.RUNNING
        else TaskType.CREATE_IMAGE_ARTIFACT
    )
    request = CaptureRequest.objects.get(image=image)
    assert request.phase == ("stop" if source_status == "running" else "capture")


@pytest.mark.django_db
def test_capture_snapshots_are_generation_specific_and_detached_from_source_mutations(
    runner, user
):
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Versioned capture source",
        runtime_type="qemu",
        status=WorkspaceStatus.STOPPED,
    )
    api = _make_process(
        workspace,
        user,
        name="api",
        command="python server.py",
        workdir="/srv/api",
        status=ProcessStatus.RUNNING,
        pid=123,
        log_path="old.log",
    )
    old_worker = _make_process(
        workspace,
        user,
        name="worker",
        command="rq worker old",
        workdir="/srv/worker",
        status=ProcessStatus.EXITED,
        exit_code=0,
    )

    _, first_task = CaptureRepository.allocate(workspace.id, "Versioned image")
    TaskRepository.complete(first_task)
    first = ImageInstance.objects.get(origin_workspace=workspace)
    first_snapshot = copy.deepcopy(first.process_definitions)
    line = first.captured_image
    first.status = ImageInstance.Status.READY
    first.runner_ref = "/images/v1.qcow2"
    first.save(update_fields=["status", "runner_ref"])
    CaptureRequest.objects.filter(image=first).update(phase="completed")
    Workspace.objects.filter(pk=workspace.pk).update(active_operation=None)

    api.command = "python server.py --new-config"
    api.workdir = "/srv/api-v2"
    api.save(update_fields=["command", "workdir"])
    old_worker.delete()
    _make_process(
        workspace,
        user,
        name="scheduler",
        command="celery beat",
        workdir="/srv/scheduler",
        status=ProcessStatus.FAILED,
    )

    _, second_task = CaptureRepository.allocate(
        workspace.id, captured_image_id=line.id, message="second generation"
    )
    second = ImageInstance.objects.exclude(id=first.id).get(origin_workspace=workspace)
    TaskRepository.complete(second_task)
    CaptureRequest.objects.filter(workspace=workspace).delete()
    workspace.delete()

    assert first.generation == 1 and second.generation == 2
    first_definitions = {item["name"]: item for item in first.process_definitions}
    second_definitions = {item["name"]: item for item in second.process_definitions}
    assert first_definitions == {item["name"]: item for item in first_snapshot}
    assert first_definitions == {
        "api": {"name": "api", "command": "python server.py", "workdir": "/srv/api"},
        "worker": {
            "name": "worker",
            "command": "rq worker old",
            "workdir": "/srv/worker",
        },
    }
    assert second_definitions == {
        "api": {
            "name": "api",
            "command": "python server.py --new-config",
            "workdir": "/srv/api-v2",
        },
        "scheduler": {
            "name": "scheduler",
            "command": "celery beat",
            "workdir": "/srv/scheduler",
        },
    }

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.process_definitions == first_snapshot
    assert {
        item["name"]: item for item in second.process_definitions
    } == second_definitions


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "creation_method",
    ["create_workspace", "create_workspace_from_image_artifact"],
)
@pytest.mark.asyncio
async def test_workspace_creation_restores_snapshot_as_fresh_exited_processes(
    runner, user, creation_method
):
    source = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Deleted process source",
        runtime_type="qemu",
        status=WorkspaceStatus.STOPPED,
    )
    source_processes = [
        _make_process(
            source,
            user,
            **definition,
            status=ProcessStatus.RUNNING,
            pid=4567,
            log_path="source-runtime.log",
            run_count=12,
            exit_code=8,
            session_id=uuid.uuid4(),
        )
        for definition in PROCESS_DEFINITIONS
    ]
    _, capture_task = CaptureRepository.allocate(source.id, "Process image")
    image = ImageInstance.objects.get(origin_workspace=source)
    assert {
        item["name"]: item for item in image.process_definitions
    } == {item["name"]: item for item in PROCESS_DEFINITIONS}
    TaskRepository.complete(capture_task)
    image.status = ImageInstance.Status.READY
    image.runner_ref = "/images/process-image.qcow2"
    image.save(update_fields=["status", "runner_ref"])
    CaptureRequest.objects.filter(image=image).delete()

    # The artifact owns its snapshot; its source workspace and process rows may
    # disappear before a later workspace is provisioned from the image.
    source.delete()
    image.refresh_from_db()
    assert image.origin_workspace_id is None
    assert not WorkspaceProcess.objects.filter(
        pk__in=[p.pk for p in source_processes]
    ).exists()

    sio = AsyncMock()
    service = RunnerService(sio_server=sio)
    workspace, task = await _create_from_image(
        service, creation_method, image, runner, user, "Restored workspace"
    )

    restored = list(WorkspaceProcess.objects.filter(workspace=workspace))
    assert task.type in {
        TaskType.CREATE_WORKSPACE,
        TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT,
    }
    restored_definitions = {
        process.name: (process.command, process.workdir) for process in restored
    }
    assert restored_definitions == {
        item["name"]: (item["command"], item["workdir"])
        for item in PROCESS_DEFINITIONS
    }
    assert {process.id for process in restored}.isdisjoint(
        {process.pk for process in source_processes}
    )
    for process in restored:
        assert process.kind == "persistent"
        assert process.created_by_id == user.id
        assert process.status == ProcessStatus.EXITED
        assert process.run_count == 0
        assert process.pid is None
        assert process.log_path == ""
        assert process.exit_code is None
        assert process.session_id is None
        assert process.ended_at is None
    emitted_events = [call.args[0] for call in sio.emit.await_args_list]
    assert not any(event == "harness:process_start" for event in emitted_events)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "creation_method",
    ["create_workspace", "create_workspace_from_image_artifact"],
)
@pytest.mark.asyncio
async def test_legacy_image_with_empty_snapshot_clones_without_processes(
    runner, user, creation_method
):
    image = _make_image(runner, user, source=None, process_definitions=[])
    service = RunnerService(sio_server=AsyncMock())
    workspace, _ = await _create_from_image(
        service, creation_method, image, runner, user, "Legacy workspace"
    )

    assert image.process_definitions == []
    assert not WorkspaceProcess.objects.filter(workspace=workspace).exists()


@pytest.mark.django_db
def test_workspace_configuration_rolls_back_workspace_processes_and_task_on_task_error(
    runner, user, monkeypatch
):
    image = _make_image(runner, user)
    workspace_id = uuid.uuid4()
    task_id = uuid.uuid4()
    before = (
        Workspace.objects.count(),
        WorkspaceProcess.objects.count(),
        Task.objects.count(),
    )
    original_create = TaskRepository.create
    processes_before_task_failure = []

    def create_task_then_fail(**kwargs):
        original_create(**kwargs)
        processes_before_task_failure.extend(
            WorkspaceProcess.objects.filter(workspace=kwargs["workspace"])
        )
        raise RuntimeError("task allocation failed after insert")

    monkeypatch.setattr(TaskRepository, "create", staticmethod(create_task_then_fail))

    with pytest.raises(RuntimeError, match="task allocation failed"):
        WorkspaceConfigurationService().create(
            workspace_fields={
                "workspace_id": workspace_id,
                "runner": runner,
                "name": "Must roll back",
                "runtime_type": image.runtime_type,
                "base_image_instance": image,
                "created_by": user,
            },
            runner=runner,
            user=user,
            organization_id=runner.organization_id,
            credentials=[],
            plugin_ids=[],
            task_id=task_id,
            operation_payload={"workspace_id": str(workspace_id)},
        )

    assert len(processes_before_task_failure) == len(PROCESS_DEFINITIONS)
    assert (
        Workspace.objects.count(),
        WorkspaceProcess.objects.count(),
        Task.objects.count(),
    ) == before
    assert not Workspace.objects.filter(pk=workspace_id).exists()
    assert not Task.objects.filter(pk=task_id).exists()
    assert not LifecycleCommand.objects.filter(task_id=task_id).exists()


@pytest.mark.django_db
def test_denied_capture_creates_no_snapshot_or_capture_artifacts(runner, user):
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Busy capture source",
        runtime_type="qemu",
        status=WorkspaceStatus.RUNNING,
        active_operation="stopping",
    )
    _make_process(
        workspace,
        user,
        name="api",
        command="python server.py",
        workdir="/srv/api",
    )
    artifact_models = (
        CapturedImage,
        ImageInstance,
        CaptureRequest,
        Task,
        LifecycleCommand,
    )
    counts = [model.objects.count() for model in artifact_models]

    with pytest.raises(ConflictError):
        CaptureRepository.allocate(workspace.id, "Denied snapshot")

    assert [model.objects.count() for model in artifact_models] == counts
    assert not CapturedImage.objects.filter(name="Denied snapshot").exists()
    assert not ImageInstance.objects.filter(origin_workspace=workspace).exists()


@pytest.mark.django_db
def test_image_process_snapshot_is_write_once(runner, user):
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Immutable snapshot source",
        runtime_type="qemu",
        status=WorkspaceStatus.STOPPED,
    )
    _make_process(
        workspace,
        user,
        name="api",
        command="python server.py",
        workdir="/srv/api",
    )
    CaptureRepository.allocate(workspace.id, "Immutable snapshot")
    image = ImageInstance.objects.get(origin_workspace=workspace)
    original = copy.deepcopy(image.process_definitions)
    image.process_definitions[0]["command"] = "overwritten"

    with pytest.raises(ValueError):
        image.save(update_fields=["process_definitions"])

    image.refresh_from_db()
    assert image.process_definitions == original


def _running_qemu_workspace(runner, user, name):
    return Workspace.objects.create(
        runner=runner,
        created_by=user,
        name=name,
        runtime_type="qemu",
        status=WorkspaceStatus.RUNNING,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_start_after_stale_preflight_does_not_insert_or_dispatch(
    runner, user, monkeypatch
):
    workspace = _running_qemu_workspace(runner, user, "Delayed start")
    sio = AsyncMock()
    service = RunnerService(sio_server=sio)
    create = service.processes.create

    def capture_before_create(**kwargs):
        CaptureRepository.allocate(workspace.id, "Start race")
        return create(**kwargs)

    monkeypatch.setattr(
        service.processes, "create", staticmethod(capture_before_create)
    )

    with pytest.raises(ConflictError):
        await service.start_process(
            workspace.id,
            "python server.py",
            name="api",
            user=user,
        )

    image = ImageInstance.objects.get(origin_workspace=workspace)
    assert image.process_definitions == []
    assert not WorkspaceProcess.objects.filter(workspace=workspace).exists()
    sio.emit.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_reconfiguration_after_stale_preflight_preserves_row_and_snapshot(
    runner, user, monkeypatch
):
    workspace = _running_qemu_workspace(runner, user, "Delayed reconfiguration")
    process = WorkspaceProcess.objects.create(
        workspace=workspace,
        created_by=user,
        name="api",
        command="python server.py",
        workdir="/srv/api",
        status=ProcessStatus.EXITED,
        run_count=0,
    )
    sio = AsyncMock()
    service = RunnerService(sio_server=sio)
    update = service.processes.update_for_restart

    def capture_before_update(process_id, **kwargs):
        CaptureRepository.allocate(workspace.id, "Reconfiguration race")
        return update(process_id, **kwargs)

    monkeypatch.setattr(
        service.processes, "update_for_restart", staticmethod(capture_before_update)
    )

    with pytest.raises(ConflictError):
        await service.start_process(
            workspace.id,
            "python server.py --new-config",
            workdir="/srv/api-v2",
            name="api",
            user=user,
        )

    image = ImageInstance.objects.get(origin_workspace=workspace)
    assert image.process_definitions == [
        {"name": "api", "command": "python server.py", "workdir": "/srv/api"}
    ]
    process.refresh_from_db()
    assert (process.command, process.workdir, process.status, process.run_count) == (
        "python server.py",
        "/srv/api",
        ProcessStatus.EXITED,
        0,
    )
    sio.emit.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_delete_after_capture_cannot_remove_snapshotted_definition(
    runner, user, monkeypatch
):
    workspace = _running_qemu_workspace(runner, user, "Delayed definition delete")
    process = WorkspaceProcess.objects.create(
        workspace=workspace,
        created_by=user,
        name="worker",
        command="rq worker",
        workdir="/srv/worker",
        status=ProcessStatus.EXITED,
        run_count=2,
    )
    service = RunnerService(sio_server=AsyncMock())
    delete = service.processes.delete_for_workspace

    def capture_before_delete(process_id, workspace_id):
        CaptureRepository.allocate(workspace.id, "Delete race")
        return delete(process_id, workspace_id)

    monkeypatch.setattr(
        service.processes, "delete_for_workspace", staticmethod(capture_before_delete)
    )

    with pytest.raises(ConflictError):
        await service.delete_process(workspace.id, process.id)

    image = ImageInstance.objects.get(origin_workspace=workspace)
    assert image.process_definitions == [
        {"name": "worker", "command": "rq worker", "workdir": "/srv/worker"}
    ]
    assert WorkspaceProcess.objects.filter(pk=process.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_process_definition_written_before_capture_is_in_snapshot(runner, user):
    workspace = _running_qemu_workspace(runner, user, "Process write first")
    process_id = uuid.uuid4()
    WorkspaceProcessRepository.create(
        process_id=process_id,
        workspace=workspace,
        created_by=user,
        name="scheduler",
        command="celery beat",
        workdir="/srv/scheduler",
        run_count=1,
    )

    CaptureRepository.allocate(workspace.id, "Process already committed")

    image = ImageInstance.objects.get(origin_workspace=workspace)
    assert image.process_definitions == [
        {
            "name": "scheduler",
            "command": "celery beat",
            "workdir": "/srv/scheduler",
        }
    ]
