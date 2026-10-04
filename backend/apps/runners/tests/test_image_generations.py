"""Immutable generation contracts; no runner filesystem changes are involved."""

import uuid

import pytest
from django.db import IntegrityError, transaction

from apps.runners.models import ImageDefinition, ImageInstance, Task, Workspace
from apps.runners.repositories import ImageGenerationRepository as Generations


@pytest.fixture
def definition(runner):
    return ImageDefinition.objects.create(
        name="Generation recipe",
        organization=runner.organization,
        runtime_type="docker",
        packages=["git"],
    )


def request(definition, runner, user, rendered="FROM ubuntu:24.04\n"):
    return Generations.request(
        definition=definition,
        runner=runner,
        created_by=user,
        rendered_input={"dockerfile_content": rendered},
    )


def finish(job, image, task, **kwargs):
    return Generations.finish(
        build_job_id=str(job.id),
        task_id=str(task.id),
        runner_id=str(job.runner_id),
        runner_ref=image.runner_ref,
        **kwargs,
    )


@pytest.mark.django_db
def test_new_generation_snapshot_failed_rebuild_and_pins(definition, runner, user):
    job, first, task = request(definition, runner, user)
    assert first.revision.recipe["packages"] == ["git"]
    assert first.revision.rendered_input == {
        "dockerfile_content": "FROM ubuntu:24.04\n"
    }
    assert str(first.id) in first.runner_ref and str(job.id) not in first.runner_ref
    assert finish(job, first, task)
    ws = Workspace.objects.create(
        runner=runner, created_by=user, base_image_instance=first
    )
    definition.packages = ["curl"]
    definition.save()
    assert job.generations.count() == 1  # Editing does not build.
    job, second, task2 = request(definition, runner, user, "FROM debian\n")
    assert first.id != second.id and first.revision_id != second.revision_id
    assert finish(job, second, task2, error="build failed")
    job.refresh_from_db()
    first.refresh_from_db()
    ws.refresh_from_db()
    assert job.current_generation_id == first.id
    assert job.status == "active" and first.status == "ready"
    assert ws.base_image_instance_id == first.id
    assert first.revision.recipe["packages"] == ["git"]
    with pytest.raises(ValueError, match="immutable"):
        first.revision.save()


@pytest.mark.django_db
def test_latest_requested_wins_out_of_order(definition, runner, user):
    job, older, old_task = request(definition, runner, user)
    job, newer, new_task = request(definition, runner, user)
    assert finish(job, newer, new_task)
    assert finish(job, older, old_task)
    job.refresh_from_db()
    older.refresh_from_db()
    assert job.current_generation_id == newer.id
    assert older.status == "ready"  # Old generations stay visible.
    assert not finish(job, older, old_task, error="late failure")
    with transaction.atomic():
        with pytest.raises(Exception, match="current"):
            Generations.validate_selection(older.id)


@pytest.mark.django_db
def test_callback_wrong_task_runner_target_and_deleting(definition, runner, user):
    job, image, task = request(definition, runner, user)
    assert not Generations.finish(
        task_id=str(task.id),
        build_job_id=str(job.id),
        runner_id=str(uuid.uuid4()),
        runner_ref=image.runner_ref,
    )
    assert not Generations.finish(
        task_id=str(uuid.uuid4()),
        build_job_id=str(job.id),
        runner_id=str(runner.id),
        runner_ref=image.runner_ref,
    )
    assert not Generations.finish(
        task_id=str(task.id),
        build_job_id=str(job.id),
        runner_id=str(runner.id),
        runner_ref="wrong target",
    )
    assert not Generations.finish(
        task_id=str(task.id),
        build_job_id=str(uuid.uuid4()),
        runner_id=str(runner.id),
        runner_ref=image.runner_ref,
    )
    task.type = "delete_image"
    task.save()
    assert not finish(job, image, task)
    task.type = "build_image"
    task.save()
    image.status = "deleting"
    image.save()
    assert not finish(job, image, task)
    image.refresh_from_db()
    assert image.status == "deleting"
    assert ImageInstance.objects.count() == 1


@pytest.mark.django_db
def test_local_constraints_and_creator_preservation(definition, runner, user):
    job, image, task = request(definition, runner, user)
    with pytest.raises(IntegrityError), transaction.atomic():
        ImageInstance.objects.filter(id=image.id).update(size_bytes=-1)
    with pytest.raises(IntegrityError), transaction.atomic():
        ImageInstance.objects.create(runner=runner, name="invalid docker capture")
    from django.db import connection

    with pytest.raises(IntegrityError), transaction.atomic():
        ImageInstance.objects.filter(id=image.id).update(creating_task_id=uuid.uuid4())
        connection.check_constraints()
    user.delete()
    image.refresh_from_db()
    assert image.created_by_id is None


@pytest.mark.django_db
def test_capture_no_fallback_wrong_workspace_and_no_revival(runner, user):
    ws = Workspace.objects.create(runner=runner, created_by=user, runtime_type="qemu")
    task = Task.objects.create(
        runner=runner, workspace=ws, type="create_image_artifact"
    )
    kwargs = dict(
        task_id=task.id,
        workspace_id=ws.id,
        runner_id=runner.id,
        runner_ref="/capture.qcow2",
    )
    assert not Generations.capture_result(**kwargs)  # Never invent an image.
    image = ImageInstance.objects.create(
        runner=runner,
        runtime_type="qemu",
        origin_workspace=ws,
        name="capture",
        status="capturing",
        creating_task=task,
    )
    assert not Generations.capture_result(**{**kwargs, "workspace_id": uuid.uuid4()})
    image.status = "deleting"
    image.save()
    assert not Generations.capture_result(**kwargs)
    image.status = "capturing"
    image.save()
    assert Generations.capture_result(**kwargs)
    assert not Generations.capture_result(**{**kwargs, "error": "late failure"})
    image.refresh_from_db()
    assert image.status == "ready"


@pytest.mark.django_db
def test_deletion_callback_exact_attempt_target(runner, user):
    task = Task.objects.create(runner=runner, type="delete_image")
    image = ImageInstance.objects.create(
        runner=runner,
        runtime_type="qemu",
        name="delete",
        status="deleting",
        deleting_task=task,
        runner_ref="/delete.qcow2",
    )
    assert Generations.delete_result(task_id=uuid.uuid4(), runner_id=runner.id) is None
    assert (
        Generations.delete_result(
            task_id=task.id, runner_id=runner.id, image_id=uuid.uuid4()
        )
        is None
    )
    assert (
        Generations.delete_result(
            task_id=task.id, runner_id=runner.id, runner_ref="/wrong.qcow2"
        )
        is None
    )
    assert Generations.delete_result(task_id=task.id, runner_id=uuid.uuid4()) is None
    assert (
        Generations.delete_result(task_id=task.id, runner_id=runner.id).id == image.id
    )
    assert (
        Generations.delete_result(
            task_id=task.id, runner_id=runner.id, error="late failure"
        )
        is None
    )


@pytest.mark.django_db
def test_selection_revalidated_under_workspace_transaction(definition, runner, user):
    from apps.runners.services.workspace_configuration import (
        WorkspaceConfigurationService,
    )
    from common.exceptions import ConflictError

    job, image, task = request(definition, runner, user)
    assert finish(job, image, task)
    image.status = "deleting"
    image.save()
    with pytest.raises(ConflictError, match="ready"):
        WorkspaceConfigurationService().create(
            workspace_fields={"base_image_instance": image},
            runner=runner,
            user=user,
            organization_id=runner.organization_id,
            credentials=[],
            plugin_ids=[],
            task_id=uuid.uuid4(),
        )
    assert not Workspace.objects.filter(base_image_instance=image).exists()


@pytest.mark.django_db
def test_identity_immutable_and_progress_task_bound(definition, runner, user):
    job, image, task = request(definition, runner, user)
    image.runner_ref = "overwritten"
    with pytest.raises(ValueError, match="immutable"):
        image.save()
    image.refresh_from_db()
    Generations.progress(
        build_job_id=job.id,
        task_id=uuid.uuid4(),
        runner_id=runner.id,
        line="wrong",
        max_chars=100,
    )
    job.refresh_from_db()
    assert job.build_log == ""
    assert finish(job, image, task)
    Generations.progress(
        build_job_id=job.id,
        task_id=task.id,
        runner_id=runner.id,
        line="late",
        max_chars=100,
    )
    job.refresh_from_db()
    assert job.status == "active" and job.build_log == ""
