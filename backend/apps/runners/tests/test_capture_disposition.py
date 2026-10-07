"""Interrupted capture children must release reservations without losing images."""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.apps import apps
from django.db import connection

from apps.credentials.services import CredentialSvc
from apps.runners.capture_repository import CaptureRepository
from apps.runners.disposition_repository import DispositionRepository
from apps.runners.models import CaptureRequest, LifecycleCommand, Task
from apps.runners.operations import OperationRepository
from apps.runners.tests.test_capture_pipeline import result
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


def interrupted_capture(service, runner, workspace, phase: str):
    """Allocate a real capture and fence its requested child phase."""
    workspace.runtime_type = "qemu"
    workspace.credentials_present = True
    workspace.save()
    _, child = CaptureRepository.allocate(workspace.id, "interrupted capture")
    if phase != "stop":
        assert result(
            service, runner, child, "workspace:stopped", credentials_present=False
        )
        CaptureRepository.tick()
    request = CaptureRequest.objects.get(workspace=workspace)
    if phase == "resume":
        assert result(
            service,
            runner,
            request.child,
            "image_artifact:created",
            image_artifact_id="/retained-capture.qcow2",
            name="interrupted capture",
            size_bytes=123,
        )
        with patch.object(
            CredentialSvc,
            "resolve_workspace_credentials",
            side_effect=RuntimeError("credential unavailable"),
        ):
            CaptureRepository.tick()
        request.refresh_from_db()
    assert request.phase == phase
    OperationRepository.intervene(request.child, "Unknown child outcome")
    command = LifecycleCommand.objects.get(task=request.child)
    evidence = {
        "status": "unknown",
        "instance_id": "exclusive-current-process",
        "execution_finished": True,
        "outcome_known": False,
        "quiescent": True,
        "identity": OperationRepository.envelope(command, {}),
    }
    return request, command, evidence


@pytest.mark.parametrize("phase", ["stop", "capture", "resume"])
def test_acknowledge_capture_closes_reservation_and_preserves_ready_image(
    service, runner, workspace, phase
):
    request, command, evidence = interrupted_capture(service, runner, workspace, phase)
    workspace.refresh_from_db()
    prior_state = (workspace.status, workspace.credentials_present)
    prior_pin = workspace.base_image_instance_id
    count = Task.objects.count()
    if phase == "stop":
        assert request.image.creating_task_id is None
    DispositionRepository.dispose(command, evidence, "acknowledge_interrupted")
    CaptureRepository.tick()
    request.refresh_from_db()
    workspace.refresh_from_db()
    assert request.phase == "failed" and request.resume_suppressed
    assert request.image.status == ("ready" if phase == "resume" else "failed")
    if phase == "resume":
        assert request.image.runner_ref == "/retained-capture.qcow2"
    assert (workspace.status, workspace.credentials_present) == prior_state
    assert workspace.base_image_instance_id == prior_pin
    assert workspace.current_task_id is None and workspace.active_operation is None
    assert Task.objects.count() == count
    # The same version line can now reserve the next generation.
    CaptureRepository.allocate(
        workspace.id, captured_image_id=request.image.captured_image_id
    )


@pytest.mark.parametrize("missing", ["quiescent", "execution_finished", "instance_id"])
def test_missing_disposition_proof_cannot_release_image_reservation(
    service, runner, workspace, missing
):
    request, command, evidence = interrupted_capture(service, runner, workspace, "stop")
    with pytest.raises(ConflictError, match="No fresh proof"):
        DispositionRepository.dispose(
            command, {**evidence, missing: False}, "acknowledge_interrupted"
        )
    request.refresh_from_db()
    workspace.refresh_from_db()
    assert request.phase == "stop" and request.image.status == "capturing"
    assert workspace.current_task_id == request.child_id


@pytest.mark.parametrize("status", ["ready", "pending_deletion", "deleting", "deleted"])
def test_stop_disposition_does_not_overwrite_finished_or_deleting_image(
    service, runner, workspace, status
):
    request, command, evidence = interrupted_capture(service, runner, workspace, "stop")
    image = request.image
    image.status = status
    image.runner_ref = "/preserved.qcow2"
    image.save()
    DispositionRepository.dispose(command, evidence, "acknowledge_interrupted")
    image.refresh_from_db()
    assert image.status == status and image.runner_ref == "/preserved.qcow2"


@pytest.mark.parametrize(
    "phase,image_status,creating_task,runner_ref,expected",
    [
        ("disposed", "capturing", False, "", "failed"),
        ("intervention", "capturing", False, "", "capturing"),
        ("disposed", "ready", False, "/retained.qcow2", "ready"),
        ("disposed", "capturing", True, "", "capturing"),
        ("disposed", "capturing", False, "/ambiguous.qcow2", "capturing"),
    ],
)
def test_data_repair_only_closes_disposed_unstarted_reservations(
    service, runner, workspace, phase, image_status, creating_task, runner_ref, expected
):
    request, command, _ = interrupted_capture(service, runner, workspace, "stop")
    CaptureRequest.objects.filter(pk=request.id).update(phase="failed")
    LifecycleCommand.objects.filter(pk=command.pk).update(phase=phase)
    image = request.image
    image.status = image_status
    image.runner_ref = runner_ref
    image.creating_task = request.child if creating_task else None
    image.save()
    migration = import_module(
        "apps.runners.migrations.0026_fail_disposed_capture_reservations"
    )
    for _ in range(2):
        migration.close_reservations(apps, SimpleNamespace(connection=connection))
        image.refresh_from_db()
        assert image.status == expected
        assert image.runner_ref == runner_ref
