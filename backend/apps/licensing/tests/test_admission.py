"""Windows license admission at create, start and recreate."""

from __future__ import annotations

import uuid
from datetime import timedelta
from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from apps.licensing.models import (
    WindowsLicensePolicy,
    WindowsProductKey,
    WindowsWorkspaceLicense,
)
from apps.licensing.tests.factories import create, image, product_key, settle, start
from apps.runners.enums import WorkspaceStatus
from apps.runners.guest_os import LINUX
from apps.runners.models import Task, Workspace
from apps.runners.services.recreate import RecreateService
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db


def _policy(organization, **fields) -> None:
    WindowsLicensePolicy.objects.update_or_create(
        organization=organization, defaults=fields
    )


def _key(licenses, organization, admin, number: int, seats: int = 1):
    view = licenses.add_key(
        org_id=organization.id,
        user=admin,
        product_key=product_key(number),
        seats=seats,
    )
    return WindowsProductKey.objects.get(pk=view.id)


def _row(workspace) -> WindowsWorkspaceLicense | None:
    return WindowsWorkspaceLicense.objects.filter(workspace=workspace).first()


def _grant(licenses, organization, admin, user, seats: int = 5) -> None:
    licenses.update_policy(
        org_id=organization.id, user=admin, changes={"user_license_seats": seats}
    )
    licenses.grant_user(org_id=organization.id, user=admin, user_id=user.id)


def test_linux_workspaces_are_never_licensed(runner, member):
    """Linux creates and starts ignore an exhausted Windows policy."""
    _policy(runner.organization, max_running=0)
    workspace = create(runner, member, LINUX)
    assert _row(workspace) is None
    start(settle(workspace, WorkspaceStatus.STOPPED))
    assert _row(workspace) is None


def test_per_device_create_takes_oldest_key_with_a_free_seat(
    runner, admin, member, licenses
):
    """Seats are counted per key and the oldest key with room is used first."""
    newer = _key(licenses, runner.organization, admin, 1, seats=2)
    older = _key(licenses, runner.organization, admin, 2, seats=1)
    WindowsProductKey.objects.filter(pk=older.pk).update(
        created_at=timezone.now() - timedelta(days=1)
    )

    first, second, third = (create(runner, member) for _ in range(3))

    assert [_row(ws).product_key_id for ws in (first, second, third)] == [
        older.id,
        newer.id,
        newer.id,
    ]
    assert {_row(ws).kind for ws in (first, second, third)} == {"device_key"}
    assert {_row(ws).activation_status for ws in (first, second, third)} == {
        "pending"
    }
    before = Workspace.objects.count()
    with pytest.raises(ConflictError) as error:
        create(runner, member)
    assert error.value.code == "windows_license_unavailable"
    assert error.value.message == (
        "All Windows licenses of this organization are in use."
    )
    # The rejected create rolled back the workspace and its task.
    assert Workspace.objects.count() == before


def test_per_device_is_the_default_and_needs_a_key(runner, member):
    with pytest.raises(ConflictError) as error:
        create(runner, member)
    assert error.value.code == "windows_license_unavailable"
    assert not Workspace.objects.exists()


def test_per_user_create_requires_an_owner_license(
    runner, admin, member, licenses
):
    _policy(runner.organization, mode="per_user")
    with pytest.raises(ConflictError) as error:
        create(runner, member)
    assert error.value.code == "windows_user_not_licensed"
    assert error.value.message == (
        "Your account has no Windows license in this organization."
    )

    _grant(licenses, runner.organization, admin, member)
    row = _row(create(runner, member))
    assert (row.kind, row.product_key_id) == ("user", None)


def test_unlicensed_create(runner, member):
    _policy(runner.organization, mode="unlicensed")
    row = _row(create(runner, member))
    assert (row.kind, row.product_key_id) == ("unlicensed", None)


def test_running_cap_on_create(runner, member):
    _policy(runner.organization, mode="unlicensed", max_running=1)
    first = create(runner, member)
    with pytest.raises(ConflictError) as error:
        create(runner, member)
    assert error.value.code == "windows_running_limit"
    assert error.value.message == (
        "The maximum number of running Windows workspaces is reached."
    )
    settle(first, WorkspaceStatus.STOPPED)
    assert _row(create(runner, member)).kind == "unlicensed"


def test_existing_windows_workspaces_get_a_row_and_count_as_running(
    runner, member
):
    """The licensing migration covers workspaces created before licensing."""
    migration = import_module(
        "apps.licensing.migrations.0002_license_existing_windows_workspaces"
    )
    legacy = {
        status: Workspace.objects.create(
            runner=runner,
            created_by=member,
            name="Legacy",
            status=status,
            base_image_instance=image(runner, member),
        )
        for status in (WorkspaceStatus.RUNNING, WorkspaceStatus.DELETED)
    }
    linux = Workspace.objects.create(
        runner=runner,
        created_by=member,
        name="Linux",
        status=WorkspaceStatus.RUNNING,
        base_image_instance=image(runner, member, LINUX),
    )

    migration.license_existing_windows_workspaces(django_apps, None)
    migration.license_existing_windows_workspaces(django_apps, None)

    assert _row(legacy[WorkspaceStatus.RUNNING]).kind == "unlicensed"
    assert _row(legacy[WorkspaceStatus.DELETED]) is None
    assert _row(linux) is None
    _policy(runner.organization, mode="unlicensed", max_running=1)
    with pytest.raises(ConflictError) as error:
        create(runner, member)
    assert error.value.code == "windows_running_limit"


def test_device_seat_always_starts_after_a_mode_change(
    runner, admin, member, licenses
):
    key = _key(licenses, runner.organization, admin, 1)
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)
    licenses.update_policy(
        org_id=runner.organization.id, user=admin, changes={"mode": "per_user"}
    )

    start(workspace)

    row = _row(workspace)
    assert (row.kind, row.product_key_id) == ("device_key", key.id)


def test_unlicensed_workspace_takes_a_seat_when_started_in_per_device_mode(
    runner, admin, member, licenses
):
    _policy(runner.organization, mode="unlicensed")
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)
    WindowsWorkspaceLicense.objects.filter(workspace=workspace).update(
        activation_status="not_activated", activation_code="0xC004F074"
    )
    _policy(runner.organization, mode="per_device")
    with pytest.raises(ConflictError) as error:
        start(workspace)
    assert error.value.code == "windows_license_unavailable"
    assert _row(workspace).kind == "unlicensed"
    assert not Task.objects.filter(workspace=workspace, type="resume_workspace")

    key = _key(licenses, runner.organization, admin, 1)
    start(workspace)

    row = _row(workspace)
    assert (row.kind, row.product_key_id) == ("device_key", key.id)
    # A new license needs a new activation.
    assert (row.activation_status, row.activation_code) == ("pending", "")


def test_revoked_owner_keeps_running_but_cannot_start_again(
    runner, admin, member, licenses
):
    _policy(runner.organization, mode="per_user")
    _grant(licenses, runner.organization, admin, member)
    workspace = settle(create(runner, member), WorkspaceStatus.RUNNING)

    licenses.revoke_user(org_id=runner.organization.id, user=admin, user_id=member.id)

    workspace.refresh_from_db()
    assert workspace.status == WorkspaceStatus.RUNNING
    assert _row(workspace).kind == "user"
    settle(workspace, WorkspaceStatus.STOPPED)
    with pytest.raises(ConflictError) as error:
        start(workspace)
    assert error.value.code == "windows_user_not_licensed"


def test_per_user_start_converts_an_unlicensed_workspace(
    runner, admin, member, licenses
):
    _policy(runner.organization, mode="unlicensed")
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)
    _policy(runner.organization, mode="per_user")
    _grant(licenses, runner.organization, admin, member)

    start(workspace)

    assert _row(workspace).kind == "user"


def test_running_cap_on_start_counts_starting_workspaces(runner, member):
    _policy(runner.organization, mode="unlicensed", max_running=1)
    first = settle(create(runner, member), WorkspaceStatus.STOPPED)
    second = settle(create(runner, member), WorkspaceStatus.STOPPED)

    start(first)  # Pending start: counts while the guest boots.
    with pytest.raises(ConflictError) as error:
        start(second)
    assert error.value.code == "windows_running_limit"

    settle(first, WorkspaceStatus.STOPPED)
    start(second)


def test_workspace_without_a_row_is_admitted_like_a_create(
    runner, admin, member, licenses
):
    key = _key(licenses, runner.organization, admin, 1)
    legacy = Workspace.objects.create(
        runner=runner,
        created_by=member,
        name="Legacy",
        status=WorkspaceStatus.STOPPED,
        base_image_instance=image(runner, member),
    )

    start(legacy)

    assert _row(legacy).product_key_id == key.id


def test_recreate_keeps_the_license_of_the_workspace(
    runner, admin, member, licenses
):
    """A reset never re-decides the license, even if a start would be refused."""
    _policy(runner.organization, mode="per_user")
    _grant(licenses, runner.organization, admin, member)
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)
    licenses.revoke_user(org_id=runner.organization.id, user=admin, user_id=member.id)
    licenses.preflight_recreate(workspace)

    RecreateService.allocate(
        workspace.id,
        workspace.base_image_instance_id,
        has_busy_agent=lambda _: False,
    )

    assert _row(workspace).kind == "user"


def test_recreate_checks_the_running_cap_only_for_a_stopped_workspace(
    runner, member, licenses
):
    _policy(runner.organization, mode="unlicensed")
    running = settle(create(runner, member), WorkspaceStatus.RUNNING)
    stopped = settle(create(runner, member), WorkspaceStatus.STOPPED)
    _policy(runner.organization, max_running=1)

    for check in (
        licenses.preflight_recreate,
        lambda ws: RecreateService.allocate(
            ws.id, ws.base_image_instance_id, has_busy_agent=lambda _: False
        ),
    ):
        with pytest.raises(ConflictError) as error:
            check(stopped)
        assert error.value.code == "windows_running_limit"

    licenses.preflight_recreate(running)
    RecreateService.allocate(
        running.id, running.base_image_instance_id, has_busy_agent=lambda _: False
    )
    # The reset keeps the slot while the old guest is gone and the new one boots.
    Workspace.objects.filter(pk=running.pk).update(status=WorkspaceStatus.STOPPED)
    with pytest.raises(ConflictError) as error:
        start(stopped)
    assert error.value.code == "windows_running_limit"


def test_preflight_rejects_without_writes(runner, member, licenses):
    with pytest.raises(ConflictError) as error:
        licenses.preflight(
            organization_id=runner.organization_id,
            guest_os="windows",
            owner_id=member.id,
        )
    assert error.value.code == "windows_license_unavailable"
    licenses.preflight(
        organization_id=runner.organization_id, guest_os="linux", owner_id=member.id
    )
    assert not WindowsLicensePolicy.objects.exists()


def test_preflight_workspace_allows_a_held_device_seat(
    runner, admin, member, licenses
):
    _key(licenses, runner.organization, admin, 1)
    workspace = settle(create(runner, member), WorkspaceStatus.STOPPED)
    # The only seat is held by this workspace itself.
    licenses.preflight_workspace(workspace)
    with pytest.raises(ConflictError):
        licenses.preflight(
            organization_id=runner.organization_id,
            guest_os="windows",
            owner_id=member.id,
            workspace_id=uuid.uuid4(),
        )
