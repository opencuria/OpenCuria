"""Windows 11 images: guest facts for clients, resource floors, the seed."""

from __future__ import annotations

import importlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async
from django.test import Client

from apps.runners.models import (
    ImageDefinition,
    ImageInstance,
    LifecycleCommand,
    Workspace,
)
from apps.runners.operations import OperationRepository
from apps.runners.provisioning import disk_size_for
from apps.runners.repositories import ImageGenerationRepository as Generations
from apps.runners.services import RunnerService
from apps.runners.services.capture import CaptureService
from apps.runners.services.qemu_resources import resolve_qemu_resources
from apps.runners.services.workspace_capacity import WorkspaceCapacityService
from apps.runners.tests.conftest import has_busy_agent
from apps.runners.tests.test_image_versions import make_line, qemu_workspace
from common.exceptions import ConflictError

seed = importlib.import_module(
    "apps.runners.migrations.0035_seed_windows_11_image_definition"
)


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


# ---------------------------------------------------------------------------
# One guest OS per captured image line
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_capture_keeps_one_guest_os_per_image_line(runner, user):
    line, _ = make_line(runner, user, name="Linux line")
    workspace = qemu_workspace(
        runner, user, built_image(runner, user), status="stopped"
    )

    with pytest.raises(ConflictError) as caught:
        CaptureService.allocate(
            workspace.id, has_busy_agent=has_busy_agent, captured_image_id=line.id
        )

    assert caught.value.code == "image_os_mismatch"
    _, task = CaptureService.allocate(
        workspace.id, "Windows line", has_busy_agent=has_busy_agent
    )
    assert task is not None


# ---------------------------------------------------------------------------
# Guest resource minimums
# ---------------------------------------------------------------------------


def runner_limits(**overrides) -> SimpleNamespace:
    values = {
        "qemu_min_vcpus": 1,
        "qemu_default_vcpus": 1,
        "qemu_max_vcpus": 8,
        "qemu_min_memory_mb": 1024,
        "qemu_default_memory_mb": 2048,
        "qemu_max_memory_mb": 16384,
        "qemu_min_disk_size_gb": 20,
        "qemu_default_disk_size_gb": 40,
        "qemu_max_disk_size_gb": 200,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def resolve(runner, requested=(None, None, None), **kwargs):
    return resolve_qemu_resources(
        runner=runner,
        qemu_vcpus=requested[0],
        qemu_memory_mb=requested[1],
        qemu_disk_size_gb=requested[2],
        **kwargs,
    )


def test_windows_defaults_are_raised_to_the_guest_minimum():
    runner = runner_limits()

    assert resolve(runner, guest_os="windows") == (2, 4096, 64)
    assert resolve(runner, guest_os="linux") == (1, 2048, 40)
    assert resolve(runner, current=(1, 3072, 50), guest_os="windows") == (2, 4096, 64)
    assert resolve(runner, current=(4, 8192, 100), guest_os="windows") == (
        4,
        8192,
        100,
    )
    assert resolve(runner, (4, 6144, 80), guest_os="windows") == (4, 6144, 80)


@pytest.mark.parametrize(
    ("requested", "message"),
    [
        ((1, None, None), "Windows workspaces need at least 2 vCPU"),
        ((None, 2048, None), "Windows workspaces need at least 4096 MiB RAM"),
        ((None, None, 40), "Windows workspaces need at least 64 GiB disk"),
    ],
)
def test_explicit_resources_below_the_guest_minimum_are_rejected(requested, message):
    with pytest.raises(ConflictError) as caught:
        resolve(runner_limits(), requested, guest_os="windows")

    assert caught.value.code == "resources_below_guest_minimum"
    assert caught.value.message == message


def test_linux_keeps_runner_range_errors():
    with pytest.raises(ConflictError) as caught:
        resolve(runner_limits(), (None, 512, None), guest_os="linux")

    assert caught.value.code == "conflict"
    assert caught.value.message == "RAM value must be between 1024 and 16384 MiB"


def test_runner_maximums_below_the_guest_minimum_are_rejected():
    runner = runner_limits(qemu_max_memory_mb=2048, qemu_max_disk_size_gb=50)

    with pytest.raises(ConflictError) as caught:
        resolve(runner, guest_os="windows")

    assert caught.value.code == "runner_limits_below_guest_minimum"
    assert "RAM 4096 MiB, disk 64 GiB" in caught.value.message
    assert resolve(runner, guest_os="linux") == (1, 2048, 40)


@pytest.mark.parametrize(
    ("guest_os", "min_disk", "requested", "expected"),
    [
        ("windows", None, 40, 64),
        ("windows", 80, 70, 80),
        ("windows", None, 100, 100),
        ("windows", 80, None, 80),
        ("windows", None, None, None),
        ("linux", None, 20, 20),
        ("linux", 30, 20, 30),
        ("linux", None, None, None),
    ],
)
def test_disk_size_for_applies_the_image_floor(guest_os, min_disk, requested, expected):
    image = SimpleNamespace(guest_os=guest_os, min_disk_size_gb=min_disk)

    assert disk_size_for(requested, image) == expected


@pytest.fixture
def lifecycle(runner):
    """Runner service with an observed Socket.IO server.

    Resource rules are independent of licensing, so the organization runs
    Windows without a license here.
    """
    from apps.licensing.models import WindowsLicensePolicy

    WindowsLicensePolicy.objects.update_or_create(
        organization_id=runner.organization_id, defaults={"mode": "unlicensed"}
    )
    return RunnerService(sio_server=AsyncMock())


@pytest.mark.django_db(transaction=True)
async def test_create_raises_unset_resources_and_rejects_low_ones(
    lifecycle, runner, user
):
    runner.qemu_default_memory_mb = 2048
    runner.qemu_default_disk_size_gb = 40
    await runner.asave()
    image = await sync_to_async(built_image)(runner, user)

    workspace, task = await lifecycle.workspace_lifecycle.create_workspace(
        name="Windows",
        repos=[],
        image_artifact_id=image.id,
        user=user,
        organization_id=runner.organization_id,
    )

    assert (workspace.qemu_vcpus, workspace.qemu_memory_mb) == (2, 4096)
    assert workspace.qemu_disk_size_gb == 64
    command = await LifecycleCommand.objects.aget(task=task)
    assert command.payload["qemu_memory_mb"] == 4096
    assert command.payload["qemu_disk_size_gb"] == 64
    with pytest.raises(ConflictError) as caught:
        await lifecycle.workspace_lifecycle.create_workspace(
            name="Too small",
            repos=[],
            image_artifact_id=image.id,
            qemu_memory_mb=2048,
            user=user,
            organization_id=runner.organization_id,
        )
    assert caught.value.code == "resources_below_guest_minimum"
    assert not await Workspace.objects.filter(name="Too small").aexists()


@pytest.mark.django_db(transaction=True)
async def test_create_fails_on_runners_too_small_for_windows(lifecycle, runner, user):
    image = await sync_to_async(built_image)(runner, user)
    runner.qemu_max_disk_size_gb = 60
    runner.qemu_default_disk_size_gb = 50
    await runner.asave()

    with pytest.raises(ConflictError) as caught:
        await lifecycle.workspace_lifecycle.create_workspace(
            name="No room",
            repos=[],
            image_artifact_id=image.id,
            user=user,
            organization_id=runner.organization_id,
        )

    assert caught.value.code == "runner_limits_below_guest_minimum"
    assert not await Workspace.objects.filter(name="No room").aexists()


@pytest.mark.django_db(transaction=True)
async def test_clone_raises_the_source_resources_to_the_guest_minimum(
    lifecycle, runner, user
):
    source = await Workspace.objects.acreate(
        runner=runner,
        name="Old windows",
        created_by=user,
        qemu_vcpus=1,
        qemu_memory_mb=2048,
        qemu_disk_size_gb=40,
    )
    line, (image,) = await sync_to_async(make_line)(runner, user, name="Win line")
    await ImageInstance.objects.filter(pk=image.pk).aupdate(
        guest_os="windows", origin_workspace=source, min_disk_size_gb=70
    )

    clone, _ = await lifecycle.workspace_lifecycle.create_workspace_from_image_artifact(
        image_artifact_id=image.id,
        name="Clone",
        user=user,
        organization_id=runner.organization_id,
    )

    assert (clone.qemu_vcpus, clone.qemu_memory_mb, clone.qemu_disk_size_gb) == (
        2,
        4096,
        70,
    )


@pytest.mark.django_db(transaction=True)
def test_resume_raises_stored_resources_to_the_guest_minimum(runner, user, lifecycle):
    image = built_image(runner, user)
    workspace = qemu_workspace(runner, user, image, status="stopped")
    Workspace.objects.filter(pk=workspace.pk).update(
        qemu_vcpus=1, qemu_memory_mb=2048, qemu_disk_size_gb=40
    )

    workspace, task = WorkspaceCapacityService.resume(workspace.id, uuid.uuid4())

    assert (workspace.qemu_vcpus, workspace.qemu_memory_mb) == (2, 4096)
    assert workspace.qemu_disk_size_gb == 64
    payload = LifecycleCommand.objects.get(task=task).payload
    assert (payload["qemu_vcpus"], payload["qemu_memory_mb"]) == (2, 4096)


@pytest.mark.django_db(transaction=True)
async def test_update_rejects_explicit_resources_below_the_guest_minimum(
    lifecycle, runner, user
):
    image = await sync_to_async(built_image)(runner, user)
    workspace = await sync_to_async(qemu_workspace)(
        runner, user, image, status="stopped"
    )
    await Workspace.objects.filter(pk=workspace.pk).aupdate(
        qemu_vcpus=2, qemu_memory_mb=4096, qemu_disk_size_gb=64
    )

    with pytest.raises(ConflictError) as caught:
        await lifecycle.workspace_lifecycle.update_workspace(
            workspace.id, qemu_memory_mb=3072, user=user
        )
    assert caught.value.code == "resources_below_guest_minimum"
    updated = await lifecycle.workspace_lifecycle.update_workspace(
        workspace.id, qemu_memory_mb=8192, user=user
    )
    assert updated.qemu_memory_mb == 8192


@pytest.mark.django_db(transaction=True)
async def test_recreate_raises_stored_resources_to_the_guest_minimum(
    lifecycle, runner, user
):
    image = await sync_to_async(built_image)(runner, user)
    workspace = await sync_to_async(qemu_workspace)(
        runner, user, image, status="stopped"
    )
    await Workspace.objects.filter(pk=workspace.pk).aupdate(
        qemu_vcpus=1, qemu_memory_mb=2048, qemu_disk_size_gb=40
    )

    workspace, _ = await lifecycle.workspace_lifecycle.recreate_workspace(
        workspace.id, image.id, user=user
    )

    await workspace.arefresh_from_db()
    assert (workspace.qemu_vcpus, workspace.qemu_memory_mb) == (2, 4096)
    assert workspace.qemu_disk_size_gb == 64


# ---------------------------------------------------------------------------
# Seeded recipe
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_windows_11_seed_is_idempotent_and_reversible():
    from django.apps import apps
    from django.db import connection

    editor = SimpleNamespace(connection=connection)
    seed.seed_windows_11(apps, editor)
    assert ImageDefinition.objects.filter(name="Windows 11").count() == 1
    seed.unseed_windows_11(apps, editor)
    assert not ImageDefinition.objects.filter(id=seed.WINDOWS_11_ID).exists()
    seed.seed_windows_11(apps, editor)
    assert ImageDefinition.objects.filter(id=seed.WINDOWS_11_ID).exists()
