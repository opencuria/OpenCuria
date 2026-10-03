"""Real independent PostgreSQL sessions, synchronized at the runner lock.

SQLite cannot establish row-lock semantics and intentionally skips this suite.
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from unittest.mock import AsyncMock

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from apps.runners.locking import lock_runner
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
    LifecycleCommand,
    Task,
    Workspace,
)
from apps.runners.operations import OperationRepository, apply_result
from apps.runners.repositories import ImageGenerationRepository as Generations
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService
from apps.runners.services.workspace_configuration import WorkspaceConfigurationService
from apps.runners.tests.test_image_deletion_coordinator import image
from apps.runners.tests.test_image_deletion_coordinator import request as delete_request
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db(transaction=True)


def race(runner, left, right):
    """Prove both connections are blocked in PostgreSQL before releasing them."""
    if connection.vendor != "postgresql":
        pytest.skip("requires real PostgreSQL row locks")
    pids = Queue()

    def worker(fn):
        connections.close_all()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids.put(cursor.fetchone()[0])
            return fn()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with transaction.atomic():
            lock_runner(runner.id)
            futures = [pool.submit(worker, fn) for fn in (left, right)]
            ids = [pids.get(timeout=5), pids.get(timeout=5)]
            assert ids[0] != ids[1]
            deadline = time.monotonic() + 5
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE pid IN (%s, %s) AND wait_event_type = 'Lock'",
                        ids,
                    )
                    blocked = cursor.fetchone()[0]
                if blocked == 2:
                    break
                assert time.monotonic() < deadline, (
                    "both workers must reach real DB lock contention"
                )
                time.sleep(0.01)
        return [f.result(timeout=15) for f in futures]


def test_selection_vs_retirement(runner, user):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL only")
    base = image(runner, user)

    def select():
        try:
            return (
                WorkspaceConfigurationService()
                .create(
                    workspace_fields={
                        "workspace_id": uuid.uuid4(),
                        "name": "Selected",
                        "runner": runner,
                        "created_by": user,
                        "base_image_instance": base,
                    },
                    runner=runner,
                    user=user,
                    organization_id=runner.organization_id,
                    credentials=[],
                    plugin_ids=[],
                    task_id=uuid.uuid4(),
                )[0]
                .id
            )
        except ConflictError:
            return None

    selected, plan = race(runner, select, lambda: delete_request(runner, user, base))
    base.refresh_from_db()
    assert base.status == "pending_deletion"
    assert plan["id"]
    assert Workspace.objects.filter(base_image_instance=base).count() == (
        1 if selected else 0
    )
    assert not Task.objects.filter(type="delete_image").exists()


def test_duplicate_operation_claim(runner, workspace):
    runner.last_heartbeat_at = timezone.now()
    runner.save()
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    OperationRepository.prepare(
        str(task.id),
        "task:stop_workspace",
        {"task_id": str(task.id), "workspace_id": str(workspace.id)},
    )
    results = race(
        runner, OperationRepository.candidates, OperationRepository.candidates
    )
    assert sorted(len(r) for r in results) == [0, 1]
    assert LifecycleCommand.objects.get(task=task).deliveries == 1


def test_duplicate_callbacks(runner, workspace):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    row = OperationRepository.prepare(
        str(task.id),
        "task:stop_workspace",
        {"task_id": str(task.id), "workspace_id": str(workspace.id)},
    )
    data = OperationRepository.envelope(
        row, {"task_id": str(task.id), "workspace_id": str(workspace.id)}
    )

    def callback():
        return apply_result(
            RunnerService(sio_server=AsyncMock()),
            str(runner.id),
            "workspace:stopped",
            data,
        )

    assert race(runner, callback, callback) == [True, True]  # terminal replay ACK
    workspace.refresh_from_db()
    task.refresh_from_db()
    assert task.status == "completed" and workspace.current_task_id is None
    assert workspace.status == "stopped"
    assert Task.objects.filter(workspace=workspace).count() == 1


def test_current_generation_promotion(runner, user):
    definition = ImageDefinition.objects.create(
        name="Concurrent builds", organization=runner.organization
    )

    def allocate():
        return Generations.request(
            definition=definition,
            runner=runner,
            created_by=user,
            rendered_input={"dockerfile_content": "FROM ubuntu\n"},
        )

    builds = race(runner, allocate, allocate)
    assert sorted(b[1].generation for b in builds) == [1, 2]
    callbacks = [
        lambda b=b: Generations.finish(
            task_id=str(b[2].id),
            build_job_id=str(b[0].id),
            runner_id=str(runner.id),
            runner_ref=b[1].runner_ref,
        )
        for b in builds
    ]
    assert race(runner, *callbacks) == [True, True]
    latest = max(builds, key=lambda b: b[1].generation)[1]
    job = ImageBuildJob.objects.get(pk=builds[0][0].id)
    assert job.current_generation_id == latest.id and job.pending_generation_id is None
    assert ImageInstance.objects.filter(build_job=job, status="ready").count() == 2


def test_cancel_and_build_completion_reconcile_under_runner_lock(runner, user):
    from apps.runners.deletion_repository import DeletionRepository

    definition = ImageDefinition.objects.create(
        name="Cancel concurrent build", organization=runner.organization
    )
    job, image, task = Generations.request(
        definition=definition, runner=runner, created_by=user, rendered_input={}
    )
    plan = DeletionRepository.request(
        runner.organization_id, user, "assignment", job.id
    )
    results = race(
        runner,
        lambda: DeletionRepository.cancel(runner.organization_id, plan["id"]),
        lambda: Generations.finish(
            task_id=task.id,
            build_job_id=job.id,
            runner_id=runner.id,
            runner_ref=image.runner_ref,
        ),
    )
    assert results[0]["phase"] == "cancelled" and results[1] is True
    job.refresh_from_db()
    image.refresh_from_db()
    assert job.status == "active" and image.status == "ready"
    assert job.current_generation_id == image.id and job.pending_generation_id is None


@pytest.mark.parametrize("launch", ["manual", "scheduled", "child"])
def test_capture_allocation_vs_harness_admission(runner, workspace, launch):
    """Capture and every harness admission contend on real PostgreSQL locks."""
    from apps.harness.models import HarnessSession
    from apps.harness.repositories import HarnessSessionRepository
    from apps.runners.capture_repository import CaptureRepository as Capture
    from apps.runners.models import CaptureRequest

    if connection.vendor != "postgresql":
        pytest.skip("requires real PostgreSQL row locks")
    workspace.runtime_type = "qemu"
    workspace.save(update_fields=["runtime_type"])
    parent = None
    if launch == "child":
        parent = HarnessSession.objects.create(
            workspace=workspace, organization_id=runner.organization_id, status="idle"
        )
    session = HarnessSession.objects.create(
        workspace=workspace,
        organization_id=runner.organization_id,
        parent=parent,
        status="idle",
    )

    def allocate():
        try:
            Capture.allocate(workspace.id, "Concurrent capture")
            return True
        except ConflictError:
            return False

    def admit():
        try:
            return HarnessSessionRepository.reserve_workspace_run(
                session.id, scheduled=launch == "scheduled"
            )
        except ConflictError:
            return False

    captured, admitted = race(runner, allocate, admit)
    assert captured != admitted  # Exactly one winner, not merely at most one.
    session.refresh_from_db()
    workspace.refresh_from_db()
    assert (session.status == "busy") == admitted
    assert CaptureRequest.objects.filter(workspace=workspace).count() == int(captured)
    assert Task.objects.filter(workspace=workspace).count() == int(captured)
    assert ImageInstance.objects.filter(origin_workspace=workspace).count() == int(
        captured
    )
    assert LifecycleCommand.objects.filter(task__workspace=workspace).count() == int(
        captured
    )
    assert (workspace.active_operation == "capturing_image") == captured
    assert (workspace.current_task_id is not None) == captured
    assert not (
        HarnessSession.objects.filter(workspace=workspace, status="busy").exists()
        and Capture.active(workspace.id)
    )


@pytest.mark.parametrize("selection", ["name", "credentials", "plugins"])
def test_stale_configuration_vs_inter_child_capture(
    runner, workspace, user, monkeypatch, selection
):
    """A stale preflight cannot overwrite the durable inter-child parent hold."""
    from unittest.mock import Mock

    from apps.runners.capture_repository import CaptureRepository as Capture
    from apps.runners.models import CaptureRequest
    from apps.runners.tests.test_capture_pipeline import result

    if connection.vendor != "postgresql":
        pytest.skip("requires real PostgreSQL row locks")
    workspace.runtime_type = "qemu"
    workspace.save(update_fields=["runtime_type"])
    configuration = WorkspaceConfigurationService()
    stale = configuration.workspaces.get_by_id(workspace.id)
    real_get = configuration.workspaces.get_by_id
    _, stop = Capture.allocate(workspace.id, "Inter-child capture")
    service = RunnerService(sio_server=AsyncMock())
    assert result(service, runner, stop, "workspace:stopped", credentials_present=False)
    workspace.refresh_from_db()
    assert workspace.current_task_id is None
    assert workspace.active_operation == "capturing_image"
    request = CaptureRequest.objects.get(workspace=workspace)

    # Only the unlocked preflight is stale; authoritative reads and both
    # contenders' locks are the real production PostgreSQL implementation.
    def stale_preflight(workspace_id, **kwargs):
        return real_get(workspace_id, **kwargs) if kwargs.get("lock") else stale

    monkeypatch.setattr(configuration.workspaces, "get_by_id", stale_preflight)
    resolve = Mock(side_effect=AssertionError("capture must reject selections"))
    monkeypatch.setattr(configuration, "_resolve_final_credentials", resolve)
    fields = {"credentials": None, "plugin_ids": None}
    fields.update(
        {
            "name": {"name": "Forbidden change"},
            "credentials": {"credentials": []},
            "plugins": {"plugin_ids": []},
        }[selection]
    )

    def update():
        with pytest.raises(ConflictError, match="lifecycle outcome unresolved"):
            configuration.update(
                workspace_id=workspace.id,
                user=user,
                organization_id=runner.organization_id,
                **fields,
            )
        return "rejected"

    assert race(runner, update, lambda: Capture.advance(request.id)) == [
        "rejected",
        None,
    ]
    resolve.assert_not_called()
    workspace.refresh_from_db()
    request.refresh_from_db()
    assert workspace.name == stale.name
    assert list(workspace.credentials.all()) == []
    assert request.phase == "capture" and request.child_id != stop.id
    assert workspace.current_task_id == request.child_id
    assert workspace.active_operation == "capturing_image"
    assert Task.objects.filter(workspace=workspace).count() == 2


@pytest.mark.parametrize("progress", ["callback", "tick", "advance"])
@pytest.mark.parametrize("failed", [False, True])
def test_duplicate_capture_completion_finishes_parent_once(
    runner, stopped_workspace, progress, failed
):
    """Queued advances and duplicate ACKs yield one terminal parent result."""
    from apps.runners.capture_repository import CaptureRepository as Capture
    from apps.runners.models import CaptureRequest
    from apps.runners.tests.test_capture_pipeline import result

    if connection.vendor != "postgresql":
        pytest.skip("requires real PostgreSQL row locks")
    workspace = stopped_workspace
    workspace.runtime_type = "qemu"
    workspace.save(update_fields=["runtime_type"])
    _, child = Capture.allocate(workspace.id, "Concurrent completion")
    request = CaptureRequest.objects.get(workspace=workspace)
    service = RunnerService(sio_server=AsyncMock())
    event = "image_artifact:failed" if failed else "image_artifact:created"
    extra = (
        {"error": "safe refusal"}
        if failed
        else {
            "image_artifact_id": "/concurrent.qcow2",
            "name": "Concurrent completion",
            "size_bytes": 123,
        }
    )
    assert result(service, runner, child, event, **extra)
    workspace.refresh_from_db()
    assert workspace.current_task_id is None
    assert workspace.active_operation == "capturing_image"

    def finish():
        if progress == "callback":
            assert result(service, runner, child, event, **extra)  # Replay ACK.
        if progress == "tick":
            return Capture.tick()
        terminal = Capture.advance(request.id)
        return [terminal] if terminal else []

    outcomes = race(runner, finish, finish)
    notifications = [item for outcome in outcomes for item in outcome]
    assert len(notifications) == 1
    request.refresh_from_db()
    workspace.refresh_from_db()
    child.refresh_from_db()
    expected = "failed" if failed else "completed"
    assert request.phase == expected and child.status == expected
    assert notifications == [
        {
            "workspace_id": str(workspace.id),
            "phase": expected,
            "diagnostic": request.diagnostic,
        }
    ]
    assert workspace.active_operation is None and workspace.current_task_id is None
    assert workspace.status == "stopped"
    assert Task.objects.filter(workspace=workspace).count() == 1
    assert LifecycleCommand.objects.filter(task__workspace=workspace).count() == 1
    assert Capture.advance(request.id) is None
    assert Capture.tick() == []


def test_credential_injection_admission_vs_capture(runner, workspace):
    """Credential synchronization and capture admit exactly one lock winner."""
    from apps.runners.capture_repository import CaptureRepository as Capture
    from apps.runners.models import CaptureRequest

    if connection.vendor != "postgresql":
        pytest.skip("requires real PostgreSQL row locks")
    workspace.runtime_type = "qemu"
    workspace.save(update_fields=["runtime_type"])
    injection_id = uuid.uuid4()

    def inject():
        try:
            TaskRepository.create(
                task_id=injection_id,
                runner=runner,
                workspace=workspace,
                task_type="inject_credentials",
            )
            return True
        except ConflictError:
            return False

    def capture():
        try:
            Capture.allocate(workspace.id, "Admission race")
            return True
        except ConflictError:
            return False

    injected, captured = race(runner, inject, capture)
    assert injected != captured
    assert Task.objects.filter(pk=injection_id).exists() == injected
    assert CaptureRequest.objects.filter(workspace=workspace).count() == int(captured)
    assert ImageInstance.objects.filter(origin_workspace=workspace).count() == int(
        captured
    )
    assert Task.objects.filter(workspace=workspace).count() == 1
    workspace.refresh_from_db()
    assert (workspace.active_operation == "capturing_image") == captured
    if injected:
        task = Task.objects.get(pk=injection_id)
        for status in ["pending", "in_progress"]:
            task.refresh_from_db()
            assert task.status == status
            with pytest.raises(ConflictError, match="credentials are synchronizing"):
                Capture.allocate(workspace.id, "Must wait for injection")
            assert not Capture.active(workspace.id)
            assert not ImageInstance.objects.filter(origin_workspace=workspace).exists()
            if status == "pending":
                TaskRepository.mark_in_progress(task)
        TaskRepository.complete(task)
        task.refresh_from_db()
        assert task.status == "completed"
        Capture.allocate(workspace.id, "After terminal injection")
        assert Capture.active(workspace.id)
    else:
        assert not inject()
        assert not Task.objects.filter(
            workspace=workspace, type="inject_credentials"
        ).exists()
        assert Capture.active(workspace.id)
