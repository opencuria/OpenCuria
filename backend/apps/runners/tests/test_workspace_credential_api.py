"""REST contract for selecting workspace credentials by ID on create and clone."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
from apps.credentials.services import CredentialSvc
from apps.organizations.models import Membership, MembershipRole
from apps.runners.models import ImageInstance, Task, Workspace
from apps.runners.services import get_runner_service
from apps.runners.tests.test_image_versions import make_line
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "rest-selected-secret"


@pytest.fixture
def member(user, organization):
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return user


@pytest.fixture
def emit(monkeypatch):
    """Use the real service path; replace only the runner transport."""
    emit = AsyncMock()
    monkeypatch.setattr(get_runner_service(), "_emit_to_runner", emit)
    return emit


def _headers(user, organization, permissions):
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="credential-selection-test",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[permission.value for permission in permissions],
    )
    return {"HTTP_X_API_KEY": token, "HTTP_X_ORGANIZATION_ID": str(organization.id)}


def _credential(organization, owner):
    service = CredentialService.objects.create(
        name="Selected token",
        slug=f"selected-{uuid.uuid4().hex[:8]}",
        credential_type="env",
        env_var_name="SELECTED_TOKEN",
    )
    OrgCredentialServiceActivation.objects.create(
        organization=organization, credential_service=service
    )
    return CredentialSvc().create_personal_credential(
        service_id=service.id,
        name="Selected credential",
        value=SECRET,
        user=owner,
        org_id=organization.id,
    )


def _image(runner, owner):
    return ImageInstance.objects.create(
        is_legacy=True,
        runner=runner,
        origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
        created_by=owner,
        runner_ref="credential-selection-image",
        name="Credential selection image",
        status=ImageInstance.Status.READY,
    )


def _post(path, body, headers):
    return Client().post(
        path, data=json.dumps(body), content_type="application/json", **headers
    )


def _dispatched_payload(emit):
    emit.assert_awaited_once()
    return next(arg for arg in emit.await_args.args if isinstance(arg, dict))


def test_create_attaches_selected_credentials_and_dispatches_secrets(
    member, runner, organization, emit
):
    credential = _credential(organization, member)
    headers = _headers(
        member,
        organization,
        [APIKeyPermission.WORKSPACES_CREATE, APIKeyPermission.CREDENTIALS_READ],
    )

    response = _post(
        "/api/v1/workspaces/",
        {
            "name": "With credentials",
            "image_artifact_id": str(_image(runner, member).id),
            "credential_ids": [str(credential.id)],
        },
        headers,
    )

    assert response.status_code == 202, response.content
    assert SECRET not in response.content.decode()
    workspace = Workspace.objects.get(pk=response.json()["workspace_id"])
    assert list(workspace.credentials.values_list("id", flat=True)) == [credential.id]
    assert _dispatched_payload(emit)["env_vars"] == {"SELECTED_TOKEN": SECRET}


def test_create_rejects_invisible_credential_without_side_effects(
    member, runner, organization, emit
):
    other = get_user_model().objects.create_user(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    foreign = _credential(organization, other)
    headers = _headers(
        member,
        organization,
        [APIKeyPermission.WORKSPACES_CREATE, APIKeyPermission.CREDENTIALS_READ],
    )
    image = _image(runner, member)
    before = (Workspace.objects.count(), Task.objects.count())

    response = _post(
        "/api/v1/workspaces/",
        {
            "name": "Foreign credential",
            "image_artifact_id": str(image.id),
            "credential_ids": [str(foreign.id)],
        },
        headers,
    )

    assert response.status_code == 404, response.content
    assert response.json()["code"] == "not_found"
    assert (Workspace.objects.count(), Task.objects.count()) == before
    emit.assert_not_awaited()


def test_clone_attaches_selected_credentials_and_dispatches_secrets(
    member, runner, organization, emit
):
    credential = _credential(organization, member)
    line, _ = make_line(runner, member)
    headers = _headers(member, organization, [APIKeyPermission.IMAGES_CLONE])

    response = _post(
        f"/api/v1/captured-images/{line.id}/workspaces/",
        {"name": "Clone with credentials", "credential_ids": [str(credential.id)]},
        headers,
    )

    assert response.status_code == 202, response.content
    workspace = Workspace.objects.get(pk=response.json()["workspace_id"])
    assert list(workspace.credentials.values_list("id", flat=True)) == [credential.id]
    assert workspace.credentials_present is True
    assert _dispatched_payload(emit)["env_vars"] == {"SELECTED_TOKEN": SECRET}
