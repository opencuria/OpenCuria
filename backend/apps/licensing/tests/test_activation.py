"""Activation input for the runner and activation results it reports."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apps.licensing.models import WindowsLicensePolicy, WindowsWorkspaceLicense
from apps.licensing.tests.factories import create, product_key, settle, start
from apps.runners.enums import TaskType, WorkspaceStatus
from apps.runners.guest_os import LINUX
from apps.runners.models import LifecycleCommand, Task
from apps.runners.operations import OperationRepository
from apps.runners.services import RunnerService
from apps.runners.services.operation_results import apply_result

pytestmark = pytest.mark.django_db


@pytest.fixture
def service() -> RunnerService:
    return RunnerService(sio_server=AsyncMock())


def _policy(organization, **fields) -> None:
    WindowsLicensePolicy.objects.update_or_create(
        organization=organization, defaults=fields
    )


def _report(service, runner, workspace, event: str, activation) -> None:
    task = Task.objects.get(pk=workspace.current_task_id)
    command = LifecycleCommand.objects.get(task=task)
    data = OperationRepository.envelope(
        command,
        {
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
            "status": "running",
            "windows_activation_result": activation,
        },
    )
    assert apply_result(service, str(runner.id), event, data)


def _row(workspace) -> WindowsWorkspaceLicense:
    return WindowsWorkspaceLicense.objects.get(workspace=workspace)


def test_activation_payload_per_license_kind(runner, admin, member, licenses):
    licenses.add_key(
        org_id=runner.organization_id, user=admin, product_key=product_key(7)
    )
    keyed = create(runner, member)
    assert licenses.activation_payload(keyed) == {
        "mode": "key",
        "product_key": product_key(7),
    }

    _policy(runner.organization, mode="per_user", kms_host="kms.example.com")
    licenses.update_policy(
        org_id=runner.organization_id,
        user=admin,
        changes={"user_license_seats": 1},
    )
    licenses.grant_user(org_id=runner.organization_id, user=admin, user_id=member.id)
    assert licenses.activation_payload(create(runner, member)) == {
        "mode": "kms",
        "kms_host": "kms.example.com",
    }

    _policy(runner.organization, mode="unlicensed")
    assert licenses.activation_payload(create(runner, member)) == {"mode": "none"}
    assert licenses.activation_payload(create(runner, member, LINUX)) is None


@pytest.mark.parametrize(
    "task_type",
    [TaskType.CREATE_WORKSPACE, TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT],
)
def test_created_result_records_the_activation(service, runner, member, task_type):
    _policy(runner.organization, mode="unlicensed")
    workspace = create(runner, member, task_type=task_type)

    _report(
        service,
        runner,
        workspace,
        "workspace:created",
        {"status": "not_activated", "code": "  0xC004F074" + "0" * 40},
    )

    row = _row(workspace)
    assert row.activation_status == "not_activated"
    assert row.activation_code == ("0xC004F074" + "0" * 40)[:32]
    assert row.checked_at is not None


def test_resumed_result_records_the_activation(service, runner, member):
    _policy(runner.organization, mode="unlicensed")
    workspace = start(settle(create(runner, member), WorkspaceStatus.STOPPED))

    _report(
        service,
        runner,
        workspace,
        "workspace:resumed",
        {"status": "activated", "code": ""},
    )

    row = _row(workspace)
    assert (row.activation_status, row.activation_code) == ("activated", "")


@pytest.mark.parametrize(
    "activation", [{"status": "licensed"}, {"code": "0x1"}, "activated", None, 7]
)
def test_unknown_activation_results_are_ignored(service, runner, member, activation):
    _policy(runner.organization, mode="unlicensed")
    workspace = create(runner, member)

    _report(service, runner, workspace, "workspace:created", activation)

    workspace.refresh_from_db()
    assert workspace.status == WorkspaceStatus.RUNNING
    row = _row(workspace)
    assert (row.activation_status, row.checked_at) == ("pending", None)
