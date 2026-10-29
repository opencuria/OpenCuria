"""Characterize capture replacement decisions and atomic workflow boundaries."""

from unittest.mock import Mock

import pytest
from django.db import connection

from apps.credentials.services import CredentialSvc
from apps.runners import capture_repository
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
from apps.runners.operations import OperationRepository
from apps.runners.services import RunnerService
from apps.runners.services.capture import CaptureService
from apps.runners.services.recreate import RecreateService
from apps.runners.tests.conftest import has_busy_agent
from apps.runners.tests.test_capture_pipeline import result
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "blocker,diagnostic,credential_checks",
    [
        ("none", "", 1),
        (
            "image",
            "Image version was deleted during capture; workspace kept as is",
            0,
        ),
        (
            "workspace",
            "Workspace changed during capture; it stays on its previous version",
            0,
        ),
        (
            "credentials",
            "Image saved as v1, but workspace credentials cannot be resolved; "
            "the workspace stays on its previous version",
            1,
        ),
        (
            "disk",
            "Image saved as v1, but it exceeds the runner disk limit; "
            "the workspace stays on its previous version",
            1,
        ),
    ],
)
def test_replacement_policy_order_and_lock_scope(
    service: RunnerService,
    runner: Runner,
    stopped_workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
    diagnostic: str,
    credential_checks: int,
) -> None:
    """Cheap blockers precede credential resolution, all under the runner lock."""
    workspace = stopped_workspace
    _, child = CaptureService.allocate(
        workspace.id, "capture", has_busy_agent=has_busy_agent
    )
    assert result(
        service,
        runner,
        child,
        "image_artifact:created",
        image_artifact_id="/capture.qcow2",
        name="capture",
        size_bytes=123,
    )
    request = CaptureRequest.objects.get(workspace=workspace)
    if blocker == "image":
        ImageInstance.objects.filter(pk=request.image_id).update(status="deleted")
    elif blocker == "workspace":
        workspace.status = "running"
        workspace.save(update_fields=["status"])
    elif blocker == "disk":
        ImageInstance.objects.filter(pk=request.image_id).update(
            min_disk_size_gb=runner.qemu_max_disk_size_gb + 1
        )

    lock = Mock(wraps=capture_repository.lock_runner)
    monkeypatch.setattr(capture_repository, "lock_runner", lock)

    def resolve(_self: CredentialSvc, locked_workspace: Workspace) -> None:
        assert connection.in_atomic_block
        assert locked_workspace.id == workspace.id
        lock.assert_called_once_with(runner.id)
        if blocker == "credentials":
            raise RuntimeError("credential revoked")

    credentials = Mock(side_effect=resolve)
    monkeypatch.setattr(
        CredentialSvc,
        "resolve_workspace_credentials",
        lambda self, ws: credentials(self, ws),
    )
    notifications = CaptureService.tick()
    request.refresh_from_db()
    assert request.phase == "completed" and request.diagnostic == diagnostic
    assert credentials.call_count == credential_checks
    assert WorkspaceRecreateRequest.objects.filter(workspace=workspace).exists() == (
        blocker == "none"
    )
    assert notifications == (
        []
        if blocker == "none"
        else [
            {
                "workspace_id": str(workspace.id),
                "phase": "completed",
                "diagnostic": diagnostic,
            }
        ]
    )


def test_failed_initial_child_rolls_back_entire_reservation(
    stopped_workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpublished initial command must leave no image line or reservation."""

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("command preparation failed")

    monkeypatch.setattr(OperationRepository, "prepare", fail)
    with pytest.raises(RuntimeError, match="command preparation failed"):
        CaptureService.allocate(
            stopped_workspace.id, "capture", has_busy_agent=has_busy_agent
        )
    assert not CapturedImage.objects.exists()
    assert not ImageInstance.objects.exists()
    assert not CaptureRequest.objects.exists()
    assert not Task.objects.exists()
    assert not LifecycleCommand.objects.exists()
    stopped_workspace.refresh_from_db()
    assert stopped_workspace.current_task_id is None
    assert stopped_workspace.active_operation is None


def test_failed_replacement_rolls_back_handoff_and_can_retry(
    service: RunnerService,
    runner: Runner,
    stopped_workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture and source repositories commit together with recreation."""
    workspace = stopped_workspace
    workspace.repos = [{"url": "https://example.com/repo.git"}]
    workspace.save(update_fields=["repos"])
    _, child = CaptureService.allocate(
        workspace.id, "capture", has_busy_agent=has_busy_agent
    )
    assert result(
        service,
        runner,
        child,
        "image_artifact:created",
        image_artifact_id="/capture.qcow2",
        name="capture",
        size_bytes=123,
    )
    request = CaptureRequest.objects.get(workspace=workspace)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("replacement allocation failed")

    with monkeypatch.context() as patch:
        patch.setattr(RecreateService, "begin", fail)
        assert CaptureService.tick() == []
    request.refresh_from_db()
    workspace.refresh_from_db()
    assert request.phase == "capture" and request.image.status == "ready"
    assert workspace.repos == [{"url": "https://example.com/repo.git"}]
    assert workspace.active_operation == "capturing_image"
    assert not WorkspaceRecreateRequest.objects.exists()
    assert Task.objects.filter(workspace=workspace).count() == 1

    assert CaptureService.tick() == []
    request.refresh_from_db()
    workspace.refresh_from_db()
    assert request.phase == "completed" and workspace.repos == []
    assert WorkspaceRecreateRequest.objects.get(workspace=workspace).phase == "remove"
