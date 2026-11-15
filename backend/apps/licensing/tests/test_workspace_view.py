"""Owners see the license state of their Windows workspaces."""

from __future__ import annotations

import pytest
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.licensing.models import WindowsLicensePolicy, WindowsWorkspaceLicense
from apps.licensing.tests.factories import create
from apps.runners.guest_os import LINUX
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db


def _get_workspace(user, organization, workspace) -> dict:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="licensing-read",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.WORKSPACES_READ.value],
    )
    response = Client().get(
        f"/api/v1/workspaces/{workspace.id}/",
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert response.status_code == 200, response.content
    return response.json()


def test_owner_sees_the_license_state_of_the_workspace(runner, member):
    WindowsLicensePolicy.objects.create(
        organization=runner.organization, mode="unlicensed"
    )
    workspace = create(runner, member)
    WindowsWorkspaceLicense.objects.filter(workspace=workspace).update(
        activation_status="failed", activation_code="0x8007232B"
    )

    body = _get_workspace(member, runner.organization, workspace)

    assert body["windows_license"] == {
        "kind": "unlicensed",
        "activation_status": "failed",
        "activation_code": "0x8007232B",
    }
    linux = create(runner, member, LINUX)
    assert _get_workspace(member, runner.organization, linux)["windows_license"] is None
