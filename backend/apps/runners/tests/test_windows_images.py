"""Windows 11 images: guest facts for clients."""

from __future__ import annotations

import uuid

import pytest
from django.test import Client

from apps.runners.models import (
    ImageDefinition,
    ImageInstance,
    LifecycleCommand,
)
from apps.runners.operations import OperationRepository
from apps.runners.repositories import ImageGenerationRepository as Generations
from apps.runners.services import RunnerService
from apps.runners.tests.test_image_versions import make_line, qemu_workspace


def built_image(runner, user, base_distro: str = "windows:11") -> ImageInstance:
    """A ready, current image version built from a recipe on ``runner``."""
    definition = ImageDefinition.objects.create(
        name=f"Recipe {uuid.uuid4().hex[:6]}",
        organization=runner.organization,
        base_distro=base_distro,
    )
    job, image, task = Generations.request(
        definition=definition, runner=runner, created_by=user, rendered_input={}
    )
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(command, command.payload)
    data.update(image_path=image.runner_ref)
    assert RunnerService().apply_operation_result(str(runner.id), "image:built", data)
    image.refresh_from_db()
    return image


# ---------------------------------------------------------------------------
# Guest facts on image versions and workspaces
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_captured_versions_report_guest_os_and_disk_floor(runner, user, organization):
    from apps.accounts.models import APIKeyPermission
    from apps.organizations.models import Membership, MembershipRole
    from apps.runners.tests.test_image_versions_api import headers

    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    line, (v1, v2) = make_line(runner, user, ("ready", "ready"), name="Win capture")
    for version, min_disk in ((v1, 40), (v2, 80)):
        ImageInstance.objects.filter(pk=version.pk).update(
            guest_os="windows", min_disk_size_gb=min_disk
        )
    make_line(runner, user, name="Linux capture")

    response = Client().get(
        "/api/v1/captured-images/",
        **headers(user, organization, [APIKeyPermission.IMAGES_READ]),
    )

    assert response.status_code == 200, response.content
    images = {image["name"]: image for image in response.json()}
    windows = images["Win capture"]
    assert windows["guest_os"] == "windows"
    assert windows["minimums"] == {"vcpus": 2, "memory_mb": 4096, "disk_size_gb": 80}
    versions = {v["version"]: v for v in windows["versions"]}
    assert versions[1]["guest_os"] == "windows"
    assert versions[1]["minimums"]["disk_size_gb"] == 64
    assert versions[2]["minimums"]["disk_size_gb"] == 80
    assert images["Linux capture"]["guest_os"] == "linux"
    assert images["Linux capture"]["versions"][0]["guest_os"] == "linux"


@pytest.mark.django_db
def test_workspace_reports_the_guest_os_of_its_image(runner, user, organization):
    from apps.accounts.models import APIKeyPermission
    from apps.organizations.models import Membership, MembershipRole
    from apps.runners.tests.test_image_versions_api import headers

    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    windows = qemu_workspace(runner, user, built_image(runner, user))
    linux = qemu_workspace(runner, user, built_image(runner, user, "ubuntu:24.04"))
    auth = headers(user, organization, [APIKeyPermission.WORKSPACES_READ])

    client = Client()
    listed = client.get("/api/v1/workspaces/", **auth)

    assert listed.status_code == 200, listed.content
    by_id = {entry["id"]: entry for entry in listed.json()}
    assert by_id[str(windows.id)]["guest_os"] == "windows"
    assert by_id[str(linux.id)]["guest_os"] == "linux"
    detail = client.get(f"/api/v1/workspaces/{windows.id}/", **auth)
    assert detail.json()["guest_os"] == "windows"
