"""An image line keeps its guest OS, so workspaces never switch OS in place."""

from __future__ import annotations

import uuid

import pytest

from apps.runners.enums import WorkspaceStatus
from apps.runners.guest_os import LINUX, WINDOWS
from apps.runners.models import CapturedImage, ImageInstance, Workspace
from apps.runners.services.capture import CaptureService
from apps.runners.services.recreate import RecreateService
from apps.runners.tests.conftest import has_busy_agent
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


def _line(runner, user, *guest_oses: str) -> list[ImageInstance]:
    line = CapturedImage.objects.create(
        organization=runner.organization, runner=runner, created_by=user, name="Line"
    )
    return [
        ImageInstance.objects.create(
            runner=runner,
            origin_type="workspace_capture",
            captured_image=line,
            generation=number,
            name="Line",
            created_by=user,
            status="ready",
            runner_ref=str(uuid.uuid4()),
            guest_os=guest_os,
        )
        for number, guest_os in enumerate(guest_oses, start=1)
    ]


def _workspace(runner, user, image: ImageInstance) -> Workspace:
    return Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Workspace",
        status=WorkspaceStatus.STOPPED,
        qemu_disk_size_gb=64,
        base_image_instance=image,
    )


def test_update_to_a_version_of_another_os_is_refused(runner, user):
    linux, windows = _line(runner, user, LINUX, WINDOWS)
    workspace = _workspace(runner, user, linux)

    with pytest.raises(ConflictError) as error:
        RecreateService.allocate(
            workspace.id, windows.id, has_busy_agent=has_busy_agent
        )

    assert error.value.code == "image_os_mismatch"
    workspace.refresh_from_db()
    assert workspace.pending_base_image_instance_id is None


def test_capture_into_a_line_of_another_os_is_refused(runner, user):
    (linux,) = _line(runner, user, LINUX)
    (windows,) = _line(runner, user, WINDOWS)
    workspace = _workspace(runner, user, windows)

    with pytest.raises(ConflictError) as error:
        CaptureService.allocate(
            workspace.id,
            captured_image_id=linux.captured_image_id,
            has_busy_agent=has_busy_agent,
        )

    assert error.value.code == "image_os_mismatch"
    assert not ImageInstance.objects.filter(origin_workspace=workspace).exists()


def test_capture_into_a_line_of_the_same_os_is_allowed(runner, user):
    (first,) = _line(runner, user, WINDOWS)
    workspace = _workspace(runner, user, first)

    CaptureService.allocate(
        workspace.id,
        captured_image_id=first.captured_image_id,
        has_busy_agent=has_busy_agent,
    )

    version = ImageInstance.objects.get(origin_workspace=workspace)
    assert (version.generation, version.guest_os) == (2, WINDOWS)
