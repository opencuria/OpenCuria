"""Interrupted builds must leave retryable metadata and keep current versions."""

from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection

from apps.organizations.models import Membership
from apps.runners.disposition_repository import DispositionRepository
from apps.runners.models import LifecycleCommand
from apps.runners.operations import OperationRepository
from apps.runners.services import RunnerService
from apps.runners.services.disposition import DispositionService
from apps.runners.services.operation_results import apply_result
from apps.runners.tests.test_image_generations import definition as definition
from apps.runners.tests.test_image_generations import finish, request

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("has_current", [False, True])
@pytest.mark.parametrize("closed_directly", [False, True])
def test_interruption_and_explicit_close_reconcile_build_assignment(
    definition, runner, user, has_current, closed_directly
):
    current = None
    if has_current:
        job, current, task = request(definition, runner, user)
        assert finish(job, current, task)
    job, image, task = request(definition, runner, user)
    if not closed_directly:
        OperationRepository.intervene(task, "Build interrupted; intervention required")
        job.refresh_from_db()
        image.refresh_from_db()
        assert image.status == "failed"
        assert job.pending_generation_id is None
        assert job.status == ("active" if has_current else "failed")
    command = LifecycleCommand.objects.get(task=task)
    state = DispositionRepository.get(runner.organization_id, task.id)
    result = DispositionService._dispose(
        state,
        {
            "identity": OperationRepository.envelope(command, {}),
            "status": "terminal",
            "instance_id": "current-runner-process",
            "execution_finished": True,
            "quiescent": True,
            "outcome_known": False,
        },
        "acknowledge_interrupted",
    )
    assert result["released"] and result["resources_preserved"]
    job.refresh_from_db()
    image.refresh_from_db()
    assert image.status == "failed"
    assert job.status == ("active" if has_current else "failed")
    assert job.pending_generation_id is None
    assert job.current_generation_id == (current.id if current else None)
    # A retry receives a new generation and independent lifecycle identity.
    job, retried, retry_task = request(definition, runner, user)
    assert retried.id != image.id and retry_task.id != task.id
    assert finish(job, retried, retry_task)
    job.refresh_from_db()
    assert job.status == "active" and job.current_generation_id == retried.id


@pytest.mark.parametrize("newer_build", [False, True])
@pytest.mark.parametrize("successful", [False, True])
def test_exact_late_result_resolves_intervention_without_overwriting_newer_build(
    definition, runner, user, newer_build, successful
):
    job, image, task = request(definition, runner, user)
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(command, command.payload)
    OperationRepository.intervene(task, "Deadline exceeded; intervention required")
    newer = None
    if newer_build:
        job, newer, newer_task = request(definition, runner, user)
    data.update(execution_finished=True, outcome_known=True)
    if successful:
        data["image_path"] = image.runner_ref
    else:
        data["error"] = "provisioning failed"
    service = RunnerService()
    assert apply_result(
        service,
        str(runner.id),
        "image:built" if successful else "image:build_failed",
        data,
    )
    job.refresh_from_db()
    image.refresh_from_db()
    assert image.status == ("ready" if successful else "failed")
    if newer_build:
        assert job.pending_generation_id == newer.id
        assert job.build_task_id == newer_task.id
        assert job.current_generation_id is None
    else:
        assert job.pending_generation_id is None
        assert job.current_generation_id == (image.id if successful else None)
        assert job.status == ("active" if successful else "failed")


def test_wrong_runner_cannot_fail_build_or_close_interruption(
    definition, runner, offline_runner, user
):
    import uuid

    from common.exceptions import NotFoundError

    job, image, task = request(definition, runner, user)
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(command, command.payload)
    assert not apply_result(
        RunnerService(),
        str(offline_runner.id),
        "image:build_failed",
        {**data, "outcome_known": False},
    )
    job.refresh_from_db()
    image.refresh_from_db()
    assert job.status == "pending" and image.status == "building"
    Membership.objects.create(user=user, organization=runner.organization, role="admin")
    with pytest.raises(NotFoundError):
        DispositionService.authorized(user, uuid.uuid4(), task.id, admin=True)


@pytest.mark.parametrize("has_current", [False, True])
@pytest.mark.parametrize("deleting", [False, True])
def test_migration_repairs_existing_stale_jobs_preserving_images_and_delete_intent(
    definition, runner, user, has_current, deleting
):
    current = None
    if has_current:
        job, current, task = request(definition, runner, user)
        assert finish(job, current, task)
    job, image, task = request(definition, runner, user)
    task.status = "failed"
    task.save(update_fields=["status"])
    if deleting:
        job.status = "pending_deletion"
        job.save(update_fields=["status"])
        image.status = "pending_deletion"
        image.save(update_fields=["status"])
    migration = import_module(
        "apps.runners.migrations.0030_reconcile_interrupted_builds"
    )
    editor = SimpleNamespace(connection=connection)
    migration.reconcile_interrupted_builds(apps, editor)
    migration.reconcile_interrupted_builds(apps, editor)
    job.refresh_from_db()
    image.refresh_from_db()
    assert job.pending_generation_id is None
    assert job.current_generation_id == (current.id if current else None)
    assert image.status == ("pending_deletion" if deleting else "failed")
    assert job.status == (
        "pending_deletion" if deleting else ("active" if has_current else "failed")
    )


def test_migration_closes_older_failed_generation_without_touching_a_live_build(
    definition, runner, user
):
    job, older, task = request(definition, runner, user)
    task.status = "failed"
    task.save(update_fields=["status"])
    job, newer, _ = request(definition, runner, user)
    migration = import_module(
        "apps.runners.migrations.0030_reconcile_interrupted_builds"
    )
    migration.reconcile_interrupted_builds(apps, SimpleNamespace(connection=connection))
    older.refresh_from_db()
    newer.refresh_from_db()
    job.refresh_from_db()
    assert older.status == "failed"
    assert newer.status == "building"
    assert job.pending_generation_id == newer.id and job.status == "pending"
