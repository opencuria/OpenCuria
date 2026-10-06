"""Versioned images: capture versions, recreate (reset/update) and retention."""

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.harness.models import HarnessSession
from apps.harness.repositories import HarnessSessionRepository
from apps.organizations.models import Membership, MembershipRole
from apps.runners.capture_repository import CaptureRepository
from apps.runners.deletion_repository import DeletionRepository
from apps.runners.enums import WorkspaceStatus
from apps.runners.image_lines import ImageLineRepository
from apps.runners.models import (
    CapturedImage,
    ImageInstance,
    LifecycleCommand,
    Runner,
    Workspace,
    WorkspaceRecreateRequest,
)
from apps.runners.recreate_repository import RecreateRepository
from apps.runners.repositories import ImageGenerationRepository
from apps.runners.retention_repository import RetentionRepository
from apps.runners.services.deletion import DeletionService
from apps.runners.tests.test_capture_pipeline import result
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock
from common.exceptions import ConflictError, NotFoundError

pytestmark = pytest.mark.django_db


def make_line(runner, user, versions=("ready",), name="Image"):
    """A captured image with one version per status (v1, v2, ...)."""
    line = CapturedImage.objects.create(
        organization=runner.organization, runner=runner, created_by=user, name=name
    )
    images = [
        ImageInstance.objects.create(
            runner=runner,
            runtime_type="qemu",
            origin_type="workspace_capture",
            captured_image=line,
            generation=number,
            name=name,
            created_by=user,
            status=status,
            runner_ref=f"{uuid.uuid4()}" if status != "failed" else "",
            size_bytes=100 * number,
        )
        for number, status in enumerate(versions, start=1)
    ]
    return line, images


def qemu_workspace(runner, user, base, status=WorkspaceStatus.RUNNING):
    return Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Versioned",
        runtime_type="qemu",
        status=status,
        qemu_disk_size_gb=20,
        base_image_instance=base,
    )


def drive(service, runner, request, event, **extra):
    request.refresh_from_db()
    assert result(service, runner, request.child, event, **extra)
    return RecreateRepository.tick()


# ---------------------------------------------------------------------------
# Capture versions
# ---------------------------------------------------------------------------


def test_new_image_capture_creates_line_with_v1(runner, user):
    ws = qemu_workspace(runner, user, None, status=WorkspaceStatus.STOPPED)
    CaptureRepository.allocate(ws.id, "  My image ", message=" first ")
    image = ImageInstance.objects.get(origin_workspace=ws)
    assert image.captured_image.name == "My image"
    assert image.generation == 1 and image.message == "first"
    assert image.min_disk_size_gb == 20 and image.status == "capturing"


def test_new_version_numbers_are_monotonic_and_never_reused(runner, user):
    line, (v1, _failed) = make_line(runner, user, ("ready", "failed"))
    ws = qemu_workspace(runner, user, v1, status=WorkspaceStatus.STOPPED)
    CaptureRepository.allocate(ws.id, captured_image_id=line.id, message="next")
    image = ImageInstance.objects.get(origin_workspace=ws)
    assert image.captured_image_id == line.id and image.generation == 3


def test_new_version_preconditions(runner, user, organization):
    other = get_user_model().objects.create_user(email="o@example.com", password="x")
    foreign_line, _ = make_line(runner, other)
    other_runner = Runner.objects.create(
        name="other", api_token_hash="h2", organization=organization
    )
    remote_line, _ = make_line(other_runner, user)
    line, (v1,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, v1, status=WorkspaceStatus.STOPPED)
    with pytest.raises(ConflictError, match="name is required"):
        CaptureRepository.allocate(ws.id, " ")
    with pytest.raises(NotFoundError):
        CaptureRepository.allocate(ws.id, captured_image_id=foreign_line.id)
    with pytest.raises(ConflictError, match="runner"):
        CaptureRepository.allocate(ws.id, captured_image_id=remote_line.id)
    CapturedImage.objects.filter(pk=line.id).update(status="pending_deletion")
    with pytest.raises(ConflictError, match="being deleted"):
        CaptureRepository.allocate(ws.id, captured_image_id=line.id)
    CapturedImage.objects.filter(pk=line.id).update(status="active")
    ImageInstance.objects.create(
        runner=runner,
        runtime_type="qemu",
        captured_image=line,
        generation=2,
        name="Image",
        status="capturing",
    )
    with pytest.raises(ConflictError, match="being captured"):
        CaptureRepository.allocate(ws.id, captured_image_id=line.id)
    assert not CapturedImage.objects.filter(name="").exists()


def test_new_workspaces_only_use_the_latest_version(runner, user):
    _, (v1, v2) = make_line(runner, user, ("ready", "ready"))
    with pytest.raises(ConflictError, match="latest"):
        ImageGenerationRepository.validate_selection(v1.id)
    assert ImageGenerationRepository.validate_selection(v2.id).id == v2.id
    v2.status = "pending_deletion"
    v2.save(update_fields=["status"])
    assert ImageGenerationRepository.validate_selection(v1.id).id == v1.id


# ---------------------------------------------------------------------------
# Recreate (reset / update)
# ---------------------------------------------------------------------------


def test_reset_keeps_identity_and_reprovisions_same_version(service, runner, user):
    _, (v1,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, v1)
    session = HarnessSession.objects.create(
        workspace=ws, organization_id=runner.organization_id, status="idle"
    )
    _, remove, request = RecreateRepository.allocate(ws.id, v1.id)
    assert request.reason == "reset" and remove.type == "remove_workspace"
    ws.refresh_from_db()
    assert ws.active_operation == "resetting"
    with pytest.raises(ConflictError):
        HarnessSessionRepository.ensure_interactions_available(ws.id)
    assert drive(service, runner, request, "workspace:removed", result="deleted") == []
    ws.refresh_from_db()
    request.refresh_from_db()
    assert ws.status == "creating" and ws.active_operation == "resetting"
    command = LifecycleCommand.objects.get(task=request.child)
    assert command.event == "task:create_workspace_from_image_artifact"
    assert command.payload["workspace_id"] == str(ws.id)
    assert command.payload["image_artifact_id"] == v1.runner_ref
    done = drive(
        service,
        runner,
        request,
        "workspace:created",
        status="running",
        credentials_present=False,
    )
    assert done == [
        {
            "workspace_id": str(ws.id),
            "phase": "completed",
            "status": "running",
            "diagnostic": "",
        }
    ]
    ws.refresh_from_db()
    assert ws.status == "running" and ws.active_operation is None
    assert ws.base_image_instance_id == v1.id
    assert ws.pending_base_image_instance_id is None
    assert HarnessSession.objects.filter(pk=session.pk, workspace=ws).exists()


def test_update_moves_to_latest_and_releases_old_pin(service, runner, user):
    _, (v1, v2) = make_line(runner, user, ("ready", "ready"))
    ws = qemu_workspace(runner, user, v1)
    _, _, request = RecreateRepository.allocate(ws.id, v2.id)
    assert request.reason == "update"
    assert ImageLineRepository.pin_counts([v1.id, v2.id]) == {v1.id: 1, v2.id: 1}
    drive(service, runner, request, "workspace:removed", result="deleted")
    ws.refresh_from_db()
    assert ws.base_image_instance_id == v2.id
    assert ImageLineRepository.pin_counts([v1.id]) == {}


def test_update_rejects_stale_and_foreign_targets(runner, user):
    _, (v1, v2, v3) = make_line(runner, user, ("ready", "ready", "ready"))
    _, (other,) = make_line(runner, user, name="Other")
    ws = qemu_workspace(runner, user, v1)
    with pytest.raises(ConflictError, match="newer"):
        RecreateRepository.allocate(ws.id, v2.id)
    with pytest.raises(ConflictError, match="latest version of its image"):
        RecreateRepository.allocate(ws.id, other.id)
    assert not WorkspaceRecreateRequest.objects.exists()
    _, _, request = RecreateRepository.allocate(ws.id, v3.id)
    assert request.reason == "update"


@pytest.mark.parametrize(
    "state", ["deleted_version", "busy_operation", "creating", "agent_busy"]
)
def test_recreate_rejects_undefined_states(runner, user, state):
    _, (v1,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, v1)
    if state == "deleted_version":
        v1.status = "pending_deletion"
        v1.save(update_fields=["status"])
    elif state == "busy_operation":
        ws.active_operation = "stopping"
        ws.save(update_fields=["active_operation"])
    elif state == "creating":
        ws.status = WorkspaceStatus.CREATING
        ws.save(update_fields=["status"])
    else:
        HarnessSession.objects.create(
            workspace=ws, organization_id=runner.organization_id, status="busy"
        )
    with pytest.raises(ConflictError):
        RecreateRepository.allocate(ws.id, v1.id)
    ws.refresh_from_db()
    assert ws.pending_base_image_instance_id is None
    assert not WorkspaceRecreateRequest.objects.exists()


def test_remove_failure_fails_workspace_keeps_target_and_allows_retry(
    service, runner, user
):
    _, (v1, v2) = make_line(runner, user, ("ready", "ready"))
    ws = qemu_workspace(runner, user, v1)
    _, _, request = RecreateRepository.allocate(ws.id, v2.id)
    done = drive(service, runner, request, "workspace:error", error="disk busy")
    assert done[0]["phase"] == "failed" and "Retry" in done[0]["diagnostic"]
    ws.refresh_from_db()
    assert ws.status == "failed" and ws.active_operation is None
    assert ws.pending_base_image_instance_id == v2.id
    assert ws.base_image_instance_id == v1.id
    _, _, retry = RecreateRepository.allocate(ws.id, v2.id)
    assert retry.phase == "remove" and retry.final_running is True


def test_create_failure_fails_workspace_on_target_and_reset_retries(
    service, runner, user
):
    _, (v1, v2) = make_line(runner, user, ("ready", "ready"))
    ws = qemu_workspace(runner, user, v1)
    _, _, request = RecreateRepository.allocate(ws.id, v2.id)
    drive(service, runner, request, "workspace:removed", result="deleted")
    done = drive(service, runner, request, "workspace:error", error="no space")
    assert done[0]["phase"] == "failed" and done[0]["status"] == "failed"
    ws.refresh_from_db()
    assert ws.base_image_instance_id == v2.id
    assert ws.pending_base_image_instance_id is None
    _, _, retry = RecreateRepository.allocate(ws.id, v2.id)
    assert retry.reason == "reset"


def test_stopped_workspace_ends_stopped_and_stop_failure_is_reported(
    service, runner, user
):
    _, (v1,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, v1, status=WorkspaceStatus.STOPPED)
    _, _, request = RecreateRepository.allocate(ws.id, v1.id)
    assert request.final_running is False
    drive(service, runner, request, "workspace:removed", result="deleted")
    assert (
        drive(
            service,
            runner,
            request,
            "workspace:created",
            status="running",
            credentials_present=False,
        )
        == []
    )
    request.refresh_from_db()
    assert request.phase == "stop"
    done = drive(service, runner, request, "workspace:error", error="stuck")
    assert done[0]["phase"] == "completed"
    assert "stopping it failed" in done[0]["diagnostic"]


def test_legacy_workspace_without_base_image_cannot_recreate(runner, user):
    ws = qemu_workspace(runner, user, None)
    _, (v1,) = make_line(runner, user)
    with pytest.raises(ConflictError, match="latest version of its image"):
        RecreateRepository.allocate(ws.id, v1.id)
    assert not WorkspaceRecreateRequest.objects.exists()
    assert ws.pending_base_image_instance_id is None


def test_heartbeat_does_not_override_status_during_recreate(service, runner, user):
    _, (v1,) = make_line(runner, user)
    ws = qemu_workspace(runner, user, v1)
    RecreateRepository.allocate(ws.id, v1.id)
    service.handle_heartbeat(
        runner, [{"workspace_id": str(ws.id), "status": "removed"}]
    )
    ws.refresh_from_db()
    assert ws.status == "running" and ws.active_operation == "resetting"


# ---------------------------------------------------------------------------
# Retention and deletion
# ---------------------------------------------------------------------------


def test_retention_keeps_latest_and_newest_and_waits_for_pins(runner, user):
    _, (v1, v2, v3, v4, _failed) = make_line(
        runner, user, ("ready", "ready", "ready", "ready", "failed")
    )
    ws = qemu_workspace(runner, user, v1)
    assert RetentionRepository.tick() == [str(v2.id)]
    v2.refresh_from_db()
    assert v2.status == "pending_deletion"
    v1.refresh_from_db()
    assert v1.status == "ready"  # Still used, reset keeps working.
    org = runner.organization
    org.image_versions_to_keep = 1
    org.save(update_fields=["image_versions_to_keep"])
    assert RetentionRepository.tick() == [str(v3.id)]
    ws.status = WorkspaceStatus.DELETED
    ws.save(update_fields=["status"])
    assert RetentionRepository.tick() == [str(v1.id)]
    v4.refresh_from_db()
    assert v4.status == "ready"
    assert RetentionRepository.tick() == []


def test_retention_labels_describe_every_ready_version(runner, user):
    _, (v1, v2, v3) = make_line(runner, user, ("ready", "ready", "ready"))
    from apps.runners.image_lines import ImageLine

    line = ImageLineRepository.line_of(v1)
    assert isinstance(line, ImageLine)
    assert ImageLineRepository.retention_labels(line, 2) == {
        v3.id: "latest",
        v2.id: "kept",
        v1.id: "expires_when_unused",
    }


def test_definition_generations_follow_the_same_retention(runner, user):
    from apps.runners.models import ImageDefinition
    from apps.runners.tests.test_image_generations import finish, request

    definition = ImageDefinition.objects.create(
        name="Retained recipe", organization=runner.organization, runtime_type="docker"
    )
    built = []
    for _ in range(3):
        job, image, task = request(definition, runner, user)
        assert finish(job, image, task)
        built.append(image)
    org = runner.organization
    org.image_versions_to_keep = 1
    org.save(update_fields=["image_versions_to_keep"])
    assert sorted(RetentionRepository.tick()) == sorted(str(i.id) for i in built[:2])
    job.refresh_from_db()
    assert job.current_generation_id == built[2].id


def test_image_deletion_retires_line_and_cancel_restores(runner, user):
    line, (v1, v2) = make_line(runner, user, ("ready", "ready"))
    plan = DeletionRepository.request(
        runner.organization_id, user, "captured_image", line.id
    )
    line.refresh_from_db()
    assert line.status == "pending_deletion"
    assert set(
        ImageInstance.objects.filter(captured_image=line).values_list(
            "status", flat=True
        )
    ) == {"pending_deletion"}
    assert ImageLineRepository.latest_for(v2) is None
    DeletionRepository.cancel(runner.organization_id, plan["id"])
    line.refresh_from_db()
    v2.refresh_from_db()
    assert line.status == "active" and v2.status == "ready"


def test_line_is_finalized_once_every_version_is_gone(runner, user):
    line, (v1,) = make_line(runner, user)
    CapturedImage.objects.filter(pk=line.id).update(status="pending_deletion")
    v1.status = "deleted"
    v1.save(update_fields=["status"])
    RetentionRepository.tick()
    line.refresh_from_db()
    assert line.status == "deleted"


def test_raising_keep_does_not_restore_deleted_versions(runner, user):
    _, (v1, v2) = make_line(runner, user, ("ready", "ready"))
    org = runner.organization
    org.image_versions_to_keep = 1
    org.save(update_fields=["image_versions_to_keep"])
    assert RetentionRepository.tick() == [str(v1.id)]
    v1.status = "deleted"
    v1.save(update_fields=["status"])
    org.image_versions_to_keep = 5
    org.save(update_fields=["image_versions_to_keep"])
    assert RetentionRepository.tick() == []
    v1.refresh_from_db()
    assert v1.status == "deleted"


def test_latest_version_is_only_removed_with_the_image(runner, user):
    Membership.objects.create(
        user=user, organization=runner.organization, role=MembershipRole.MEMBER
    )
    _, (v1, v2) = make_line(runner, user, ("ready", "ready"))
    with pytest.raises(ConflictError, match="deleting the image"):
        DeletionService().request(user, runner.organization_id, "image", v2.id)
    plan = DeletionService().request(user, runner.organization_id, "image", v1.id)
    assert plan["target_id"] == str(v1.id)
