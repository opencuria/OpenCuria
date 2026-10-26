"""Characterize reservation reads independently of lifecycle phase progression."""

import uuid

import pytest
from django.utils import timezone

from apps.runners.models import (
    CapturedImage,
    CaptureRequest,
    ImageInstance,
    LifecycleCommand,
    Runner,
    Task,
    Workspace,
    WorkspaceRecreateRequest,
)
from apps.runners.workspace_operation_repository import WorkspaceOperationRepository

pytestmark = pytest.mark.django_db


@pytest.fixture
def capture_request(workspace: Workspace) -> CaptureRequest:
    """Create reservation state without invoking the capture coordinator."""
    line = CapturedImage.objects.create(
        runner=workspace.runner,
        organization=workspace.runner.organization,
        name="Capture",
    )
    image = ImageInstance.objects.create(
        runner=workspace.runner,
        captured_image=line,
        generation=1,
        name="Capture",
        status="capturing",
    )
    return CaptureRequest.objects.create(workspace=workspace, image=image)


@pytest.mark.parametrize("phase", ["stop", "capture", "resume", "completed", "failed"])
def test_capture_reservation_lasts_until_terminal_phase(
    capture_request: CaptureRequest, phase: str
) -> None:
    """Child status does not release its enclosing capture reservation."""
    capture_request.phase = phase
    capture_request.save(update_fields=["phase"])
    assert WorkspaceOperationRepository.capture_active(
        capture_request.workspace_id
    ) == (phase not in {"completed", "failed"})


@pytest.mark.parametrize("phase", ["remove", "create", "stop", "completed", "failed"])
def test_recreate_reservation_lasts_until_terminal_phase(
    workspace: Workspace, phase: str
) -> None:
    """The live request supplies its reason even between child tasks."""
    request = WorkspaceRecreateRequest.objects.create(
        workspace=workspace, reason="reset", phase=phase
    )
    live = WorkspaceOperationRepository.live_recreate(workspace.id)
    active = phase not in {"completed", "failed"}
    assert WorkspaceOperationRepository.recreate_active(workspace.id) == active
    assert (live.id if live else None) == (request.id if active else None)


@pytest.mark.parametrize("phase", ["remove", "create", "stop", "completed", "failed"])
def test_recreate_ownership_requires_its_exact_unfinished_child(
    workspace: Workspace, phase: str
) -> None:
    """A previous or unrelated task cannot be handled as the recreate child."""
    child = Task.objects.create(
        runner=workspace.runner, workspace=workspace, type="remove_workspace"
    )
    other = Task.objects.create(
        runner=workspace.runner, workspace=workspace, type="remove_workspace"
    )
    WorkspaceRecreateRequest.objects.create(
        workspace=workspace, reason="reset", phase=phase, child=child
    )
    assert WorkspaceOperationRepository.owns_recreate_child(child.id) == (
        phase not in {"completed", "failed"}
    )
    assert not WorkspaceOperationRepository.owns_recreate_child(other.id)


@pytest.mark.parametrize("fallback", [None, "starting"])
def test_absent_reservations_keep_the_fallback(fallback: str | None) -> None:
    """Missing workspaces are harmless reads, including frontend race windows."""
    workspace_id = uuid.uuid4()
    assert not WorkspaceOperationRepository.capture_active(workspace_id)
    assert not WorkspaceOperationRepository.recreate_active(workspace_id)
    assert WorkspaceOperationRepository.live_recreate(workspace_id) is None
    assert WorkspaceOperationRepository.project(workspace_id, fallback) == fallback


@pytest.mark.parametrize(
    "reason, operation",
    [
        ("capture", "capturing_image"),
        ("reset", "resetting"),
        ("update", "updating"),
    ],
)
@pytest.mark.parametrize("phase", ["remove", "create", "stop", "completed", "failed"])
def test_recreate_reason_projects_one_operation_across_children(
    workspace: Workspace, reason: str, operation: str, phase: str
) -> None:
    """Only terminal parent phases return control to the caller's fallback."""
    WorkspaceRecreateRequest.objects.create(
        workspace=workspace, reason=reason, phase=phase
    )
    expected = "starting" if phase in {"completed", "failed"} else operation
    assert (
        WorkspaceOperationRepository.project(str(workspace.id), "starting") == expected
    )


@pytest.mark.parametrize("capture_phase", ["capture", "completed", "failed"])
@pytest.mark.parametrize("command_phase", ["delivery", "executing", "intervention"])
def test_capture_priority_and_current_child_intervention(
    capture_request: CaptureRequest, capture_phase: str, command_phase: str
) -> None:
    """An active capture wins over recreate; an intervening child clears both."""
    workspace = capture_request.workspace
    capture_request.phase = capture_phase
    capture_request.save(update_fields=["phase"])
    WorkspaceRecreateRequest.objects.create(workspace=workspace, reason="update")
    child = Task.objects.create(
        runner=workspace.runner, workspace=workspace, type="stop_workspace"
    )
    LifecycleCommand.objects.create(
        task=child, phase=command_phase, deadline_at=timezone.now()
    )
    workspace.current_task = child
    workspace.save(update_fields=["current_task"])

    expected = "capturing_image" if capture_phase == "capture" else "updating"
    if command_phase == "intervention":
        expected = None
    assert WorkspaceOperationRepository.project(workspace.id, "starting") == expected


@pytest.mark.parametrize("reservation", ["capture", "recreate", "none"])
def test_intervention_applies_only_to_the_current_child(
    workspace: Workspace, capture_request: CaptureRequest, reservation: str
) -> None:
    """Past child failures never hide a new operation or an unowned fallback."""
    if reservation != "capture":
        capture_request.phase = "completed"
        capture_request.save(update_fields=["phase"])
    if reservation == "recreate":
        WorkspaceRecreateRequest.objects.create(workspace=workspace, reason="reset")
    old_child = Task.objects.create(
        runner=workspace.runner, workspace=workspace, type="stop_workspace"
    )
    LifecycleCommand.objects.create(
        task=old_child, phase="intervention", deadline_at=timezone.now()
    )
    child = Task.objects.create(
        runner=workspace.runner, workspace=workspace, type="resume_workspace"
    )
    workspace.current_task = child
    workspace.save(update_fields=["current_task"])
    expected = {
        "capture": "capturing_image",
        "recreate": "resetting",
        "none": "starting",
    }[reservation]
    assert WorkspaceOperationRepository.project(workspace.id, "starting") == expected

    # The same intervening command is now current. It still cannot suppress a
    # fallback when neither durable parent owns the workspace.
    workspace.current_task = old_child
    workspace.save(update_fields=["current_task"])
    assert WorkspaceOperationRepository.project(workspace.id, "starting") == (
        "starting" if reservation == "none" else None
    )


def test_reservations_are_scoped_to_their_workspace(
    capture_request: CaptureRequest, runner: Runner
) -> None:
    """Another workspace's parent reservations cannot block or label this one."""
    WorkspaceRecreateRequest.objects.create(
        workspace=capture_request.workspace, reason="reset"
    )
    other = Workspace.objects.create(
        runner=runner, name="Other", created_by=capture_request.workspace.created_by
    )
    assert not WorkspaceOperationRepository.capture_active(other.id)
    assert not WorkspaceOperationRepository.recreate_active(other.id)
    assert WorkspaceOperationRepository.live_recreate(other.id) is None
    assert WorkspaceOperationRepository.project(other.id, "starting") == "starting"
