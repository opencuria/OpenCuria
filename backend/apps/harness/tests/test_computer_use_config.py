"""Computer Use configuration, tenant authorization and migration contracts."""

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.models import ComputerUseConfig
from apps.harness.services import ComputerUseConfigService
from apps.organizations.models import Membership, MembershipRole, Organization
from common.utils import generate_api_token, hash_token


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"agents-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


@pytest.fixture
def computer_use_setup(db):
    """Org + owner + outsider for Computer Use config tests."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"ComputerUse {uuid.uuid4().hex[:6]}",
        slug=f"agent-s-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"as-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    outsider = user_model.objects.create_user(
        email=f"as-out-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    return {"org": org, "owner": owner, "outsider": outsider}


URL = "/api/v1/computer-use-config/"
pytestmark = pytest.mark.django_db


def test_defaults_and_partial_update(computer_use_setup):
    service = ComputerUseConfigService()
    org = computer_use_setup["org"]
    assert service.get_or_default(org.id) == service.DEFAULTS
    assert not ComputerUseConfig.objects.exists()
    assert service.save_config(org.id, {"enable_recording": True}) == {
        **service.DEFAULTS,
        "enable_recording": True,
    }
    assert service.save_config(org.id, {"max_steps": 23})["enable_recording"] is True
    assert ComputerUseConfig.objects.count() == 1


@pytest.mark.parametrize(
    "values",
    [
        {"max_steps": 0},
        {"max_steps": True},
        {"max_steps": 1001},
        {"screenshot_max_dimension": 1},
        {"enable_recording": "true"},
        {"grounding_model": "obsolete"},
    ],
)
def test_invalid_values_do_not_write(computer_use_setup, values):
    with pytest.raises(ValueError):
        ComputerUseConfigService().save_config(computer_use_setup["org"].id, values)
    assert not ComputerUseConfig.objects.exists()


def test_api_permissions_and_tenant_boundary(computer_use_setup):
    fixture = computer_use_setup
    read = _client(
        user=fixture["owner"],
        org=fixture["org"],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    run = _client(
        user=fixture["owner"],
        org=fixture["org"],
        permissions=[APIKeyPermission.HARNESS_RUN.value],
    )
    assert read.get(URL).status_code == 200
    assert read.put(URL, data="{}", content_type="application/json").status_code == 403
    assert run.get(URL).status_code == 403
    response = run.put(
        URL,
        data=json.dumps({"enable_recording": True}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["enable_recording"] is True
    stranger = _client(
        user=fixture["outsider"],
        org=fixture["org"],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    assert stranger.get(URL).status_code == 404


def test_retired_api_and_strict_partial_payload(computer_use_setup):
    """Retired fields are explicit errors and never create configuration rows."""
    fixture = computer_use_setup
    client = _client(
        user=fixture["owner"],
        org=fixture["org"],
        permissions=[
            APIKeyPermission.HARNESS_READ.value,
            APIKeyPermission.HARNESS_RUN.value,
        ],
    )
    assert client.get("/api/v1/agent-s-config/").status_code == 410
    assert (
        client.put(
            "/api/v1/agent-s-config/", data="{}", content_type="application/json"
        ).status_code
        == 410
    )
    for payload in ({"grounding_model": "obsolete"}, {"enable_recording": "false"}):
        assert (
            client.put(
                URL, data=json.dumps(payload), content_type="application/json"
            ).status_code
            == 422
        )
    assert not ComputerUseConfig.objects.exists()
