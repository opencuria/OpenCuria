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
                        "SELECT count(*) FROM pg_stat_activity WHERE pid IN (%s, %s) AND wait_event_type = 'Lock'",
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
