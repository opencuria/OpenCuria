"""Windows activation input is a delivery secret, like resolved credentials."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import async_to_sync
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.licensing.models import (
    WindowsLicensePolicy,
    WindowsProductKey,
    WindowsWorkspaceLicense,
)
from apps.organizations.models import Membership, MembershipRole
from apps.runners.enums import TaskType, WorkspaceStatus
from apps.runners.guest_os import LINUX, WINDOWS
from apps.runners.models import ImageInstance, LifecycleCommand, Workspace
from apps.runners.operations import SECRET_PAYLOAD_KEYS, WINDOWS_ACTIVATION_KEY
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService, get_runner_service
from apps.runners.services.recovery import RecoveryService
from common.utils import encrypt_value, generate_api_token, hash_token

pytestmark = pytest.mark.django_db

PRODUCT_KEY = "ABCDE-FGHIJ-KLMNO-PQRST-VWXYZ"
KEY_ACTIVATION = {"mode": "key", "product_key": PRODUCT_KEY}


@pytest.fixture
def member(user, organization):
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return user


@pytest.fixture
def key(organization) -> WindowsProductKey:
    """A per-device pool (the default mode) with free seats."""
    return WindowsProductKey.objects.create(
        organization=organization,
        encrypted_key=encrypt_value(PRODUCT_KEY),
        key_suffix=PRODUCT_KEY[-5:],
        fingerprint=hash_token(PRODUCT_KEY),
        seats=5,
    )


@pytest.fixture
def emit(monkeypatch):
    """Use the real service path; replace only the runner transport."""
    emit = AsyncMock()
    monkeypatch.setattr(get_runner_service().runner_transport, "emit_to_runner", emit)
    return emit


def _image(runner, owner, guest_os: str) -> ImageInstance:
    return ImageInstance.objects.create(
        is_legacy=True,
        runner=runner,
        origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
        created_by=owner,
        runner_ref=f"{guest_os}-image",
        name=f"{guest_os} image",
        status=ImageInstance.Status.READY,
        guest_os=guest_os,
    )


def _workspace(runner, owner, *, status=WorkspaceStatus.STOPPED, key=None):
    """A Windows workspace holding a seat of ``key`` (or an unlicensed row)."""
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=owner,
        name="Windows",
        status=status,
        base_image_instance=_image(runner, owner, WINDOWS),
    )
    WindowsWorkspaceLicense.objects.create(
        workspace=workspace,
        kind="unlicensed" if key is None else "device_key",
        product_key=key,
    )
    return workspace


def _headers(user, organization, permission) -> dict:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="windows-activation",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[permission.value],
    )
    return {"HTTP_X_API_KEY": token, "HTTP_X_ORGANIZATION_ID": str(organization.id)}


def _dispatched(emit) -> dict:
    emit.assert_awaited_once()
    return next(arg for arg in emit.await_args.args if isinstance(arg, dict))


def _persisted(task_id) -> dict:
    payload = LifecycleCommand.objects.get(task_id=task_id).payload
    assert WINDOWS_ACTIVATION_KEY not in payload
    assert PRODUCT_KEY not in json.dumps(payload)
    return payload


def test_activation_is_a_shared_secret_payload_key():
    assert WINDOWS_ACTIVATION_KEY in SECRET_PAYLOAD_KEYS
    assert {"env_vars", "files", "ssh_keys"} < set(SECRET_PAYLOAD_KEYS)


@pytest.mark.parametrize(("guest_os", "seats"), [(WINDOWS, 1), (LINUX, 0)])
def test_create_dispatches_activation_without_persisting_it(
    member, runner, organization, key, emit, guest_os, seats
):
    response = Client().post(
        "/api/v1/workspaces/",
        data=json.dumps(
            {
                "name": "Activated",
                "image_artifact_id": str(_image(runner, member, guest_os).id),
            }
        ),
        content_type="application/json",
        **_headers(member, organization, APIKeyPermission.WORKSPACES_CREATE),
    )

    assert response.status_code == 202, response.content
    assert PRODUCT_KEY not in response.content.decode()
    dispatched = _dispatched(emit)
    assert dispatched["guest_os"] == guest_os
    if guest_os == WINDOWS:
        assert dispatched[WINDOWS_ACTIVATION_KEY] == KEY_ACTIVATION
    else:
        assert WINDOWS_ACTIVATION_KEY not in dispatched
    assert _persisted(dispatched["task_id"])["guest_os"] == guest_os
    assert WindowsWorkspaceLicense.objects.filter(product_key=key).count() == seats


def test_create_is_refused_without_a_free_seat(member, runner, organization, emit):
    response = Client().post(
        "/api/v1/workspaces/",
        data=json.dumps(
            {
                "name": "No seat",
                "image_artifact_id": str(_image(runner, member, WINDOWS).id),
            }
        ),
        content_type="application/json",
        **_headers(member, organization, APIKeyPermission.WORKSPACES_CREATE),
    )

    assert response.status_code == 409, response.content
    assert response.json()["code"] == "windows_license_unavailable"
    assert not Workspace.objects.exists()
    emit.assert_not_awaited()


def test_resume_dispatches_activation_without_persisting_it(
    member, runner, organization, key, emit
):
    workspace = _workspace(runner, member, key=key)

    response = Client().post(
        f"/api/v1/workspaces/{workspace.id}/resume/",
        **_headers(member, organization, APIKeyPermission.WORKSPACES_RESUME),
    )

    assert response.status_code == 202, response.content
    dispatched = _dispatched(emit)
    assert dispatched[WINDOWS_ACTIVATION_KEY] == KEY_ACTIVATION
    assert dispatched["guest_os"] == WINDOWS
    assert _persisted(dispatched["task_id"])["guest_os"] == WINDOWS


def test_resume_is_refused_by_the_running_cap(member, runner, organization, emit):
    WindowsLicensePolicy.objects.create(
        organization=organization, mode="unlicensed", max_running=1
    )
    _workspace(runner, member, status=WorkspaceStatus.RUNNING)
    workspace = _workspace(runner, member)

    response = Client().post(
        f"/api/v1/workspaces/{workspace.id}/resume/",
        **_headers(member, organization, APIKeyPermission.WORKSPACES_RESUME),
    )

    assert response.status_code == 409, response.content
    assert response.json()["code"] == "windows_running_limit"
    emit.assert_not_awaited()


def test_transport_delivers_live_activation_only(runner, user, key):
    sio = AsyncMock()
    workspace = _workspace(runner, user, key=key)
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type=TaskType.RESUME_WORKSPACE,
    )

    async_to_sync(RunnerService(sio_server=sio).runner_transport.emit_to_runner)(
        runner,
        "task:resume_workspace",
        {
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
            WINDOWS_ACTIVATION_KEY: KEY_ACTIVATION,
        },
    )

    event, envelope = sio.emit.await_args.args
    assert event == "task:resume_workspace"
    assert envelope[WINDOWS_ACTIVATION_KEY] == KEY_ACTIVATION
    assert envelope["guest_os"] == WINDOWS
    assert envelope["operation_id"] == str(task.id)
    _persisted(task.id)


def test_recovery_resolves_activation_for_every_redelivery(runner, user, key):
    windows = _workspace(runner, user, key=key)
    linux = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Linux",
        status=WorkspaceStatus.STOPPED,
        base_image_instance=_image(runner, user, LINUX),
    )
    for workspace in (windows, linux):
        TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=runner,
            workspace=workspace,
            task_type=TaskType.RESUME_WORKSPACE,
        )
    transport = AsyncMock()

    async_to_sync(RecoveryService().tick)(transport)

    delivered = {
        payload["workspace_id"]: payload
        for event, payload in (call.args for call in transport.emit.await_args_list)
        if event == "task:resume_workspace"
    }
    assert delivered[str(windows.id)][WINDOWS_ACTIVATION_KEY] == KEY_ACTIVATION
    assert delivered[str(windows.id)]["guest_os"] == WINDOWS
    assert WINDOWS_ACTIVATION_KEY not in delivered[str(linux.id)]
    assert delivered[str(linux.id)]["guest_os"] == LINUX
    for payload in delivered.values():
        _persisted(payload["task_id"])
