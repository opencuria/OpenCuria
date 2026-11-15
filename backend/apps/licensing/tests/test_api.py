"""Admin-only REST API for Windows licensing."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from django.db.models import RestrictedError
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.licensing.models import (
    WindowsLicensePolicy,
    WindowsProductKey,
    WindowsUserLicense,
    WindowsWorkspaceLicense,
)
from apps.licensing.tests.factories import create, make_user, product_key, settle
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import WorkspaceStatus
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db

READ_WRITE = [APIKeyPermission.ORGANIZATIONS_READ, APIKeyPermission.ORGANIZATIONS_WRITE]


class Api:
    """A client authenticated with an API key of the given scopes."""

    def __init__(self, user: Any, organization: Organization, scopes) -> None:
        token = generate_api_token()
        APIKey.objects.create(
            user=user,
            name="licensing-test",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            permissions=[scope.value for scope in scopes],
        )
        self.base = f"/api/v1/licensing/organizations/{organization.id}/windows/"
        self.headers = {
            "HTTP_X_API_KEY": token,
            "HTTP_X_ORGANIZATION_ID": str(organization.id),
        }
        self.client = Client()

    def __call__(self, method: str, path: str = "", body: dict | None = None):
        return getattr(self.client, method)(
            self.base + path,
            data=json.dumps(body) if body is not None else None,
            content_type="application/json",
            **self.headers,
        )


@pytest.fixture
def api(admin, organization) -> Api:
    return Api(admin, organization, READ_WRITE)


def _add_key(api: Api, number: int = 1, **body) -> dict:
    response = api("post", "keys/", {"product_key": product_key(number), **body})
    assert response.status_code == 201, response.content
    return response.json()


def _error(response, status: int, code: str) -> str:
    assert response.status_code == status, response.content
    assert response.json()["code"] == code
    return response.json()["detail"]


# -- Permissions ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "", None),
        ("patch", "", {"mode": "unlicensed"}),
        ("post", "keys/", {"product_key": product_key(1)}),
        ("put", "users/1/", None),
        ("delete", "users/1/", None),
    ],
)
def test_members_are_refused(member, organization, method, path, body):
    response = Api(member, organization, READ_WRITE)(method, path, body)
    _error(response, 403, "authentication_error")
    assert not WindowsLicensePolicy.objects.filter(mode="unlicensed").exists()
    assert not WindowsProductKey.objects.exists()


def test_api_key_scopes_are_required(admin, organization):
    unscoped = Api(admin, organization, [])
    _error(unscoped("get"), 403, "permission_denied")
    read_only = Api(admin, organization, [APIKeyPermission.ORGANIZATIONS_READ])
    assert read_only("get").status_code == 200
    _error(read_only("patch", "", {"mode": "unlicensed"}), 403, "permission_denied")
    _error(
        read_only("post", "keys/", {"product_key": product_key(1)}),
        403,
        "permission_denied",
    )
    assert not WindowsProductKey.objects.exists()


def test_other_organizations_are_not_found(admin):
    other = Organization.objects.create(name="Other", slug=f"o-{uuid.uuid4().hex}")
    _error(Api(admin, other, READ_WRITE)("get"), 404, "not_found")


# -- Policy --------------------------------------------------------------------


def test_default_view_lists_members_without_secrets(api, admin, member):
    body = api("get").json()
    assert body["mode"] == "per_device"
    assert body["kms_host"] == "" and body["user_license_seats"] == 0
    assert body["max_running"] is None and body["product_keys"] == []
    assert body["usage"] == {
        "windows_workspaces": 0,
        "running": 0,
        "device_key": 0,
        "user": 0,
        "unlicensed": 0,
    }
    assert body["members"] == [
        {"user_id": str(admin.id), "label": "Ada", "licensed": False},
        {"user_id": str(member.id), "label": "Max", "licensed": False},
    ]
    assert member.email not in json.dumps(body)


def test_inactive_members_are_not_listed(api, organization):
    inactive = make_user("inactive", first_name="Ina")
    inactive.is_active = False
    inactive.save(update_fields=["is_active"])
    Membership.objects.create(
        user=inactive, organization=organization, role=MembershipRole.MEMBER
    )
    labels = [member["label"] for member in api("get").json()["members"]]
    assert labels == ["Ada"]
    api("patch", "", {"user_license_seats": 1})
    _error(api("put", f"users/{inactive.id}/"), 404, "not_found")


def test_update_policy(api):
    body = api(
        "patch",
        "",
        {
            "mode": "per_user",
            "kms_host": " kms.example.com:1688 ",
            "user_license_seats": 3,
            "max_running": 2,
        },
    ).json()
    assert (body["mode"], body["kms_host"]) == ("per_user", "kms.example.com:1688")
    assert (body["user_license_seats"], body["max_running"]) == (3, 2)

    body = api("patch", "", {"kms_host": "[2001:db8::1]:1688"}).json()
    assert body["kms_host"] == "[2001:db8::1]:1688"
    assert body["max_running"] == 2  # Unsent fields stay.
    assert api("patch", "", {"max_running": None}).json()["max_running"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "per_seat"},
        {"kms_host": "kms host"},
        {"kms_host": "kms.example.com:70000"},
        {"user_license_seats": -1},
        {"max_running": -1},
    ],
)
def test_invalid_policy_is_rejected(api, body):
    _error(api("patch", "", body), 400, "validation_error")
    assert not WindowsLicensePolicy.objects.exists()


def test_mode_change_keeps_existing_workspace_licenses(api, runner, member):
    _add_key(api)
    workspace = create(runner, member)
    assert api("patch", "", {"mode": "unlicensed"}).status_code == 200
    row = WindowsWorkspaceLicense.objects.get(workspace=workspace)
    assert row.kind == "device_key" and row.product_key_id is not None


# -- Product keys --------------------------------------------------------------


def test_add_key_returns_only_its_suffix(api):
    response = api(
        "post",
        "keys/",
        {
            "product_key": " k0001-aaaaa-bbbbb-ccccc-ddd dd ",
            "label": " MAK ",
            "seats": 3,
        },
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert (body["label"], body["key_suffix"], body["seats"]) == ("MAK", "DDDDD", 3)
    assert (body["used_seats"], body["assignments"]) == (0, [])
    assert product_key(1) not in response.content.decode()
    key = WindowsProductKey.objects.get()
    assert product_key(1) not in key.encrypted_key
    assert product_key(1) not in api("get").content.decode()


@pytest.mark.parametrize(
    "value", ["K0001-AAAAA-BBBBB-CCCCC", "K0001_AAAAA_BBBBB_CCCCC_DDDDD", "K0001"]
)
def test_malformed_key_is_rejected(api, value):
    _error(api("post", "keys/", {"product_key": value}), 400, "windows_key_invalid")
    assert not WindowsProductKey.objects.exists()


def test_key_seats_must_be_positive(api):
    response = api("post", "keys/", {"product_key": product_key(1), "seats": 0})
    _error(response, 400, "validation_error")


def test_duplicate_key_is_rejected(api):
    _add_key(api)
    response = api("post", "keys/", {"product_key": product_key(1).lower()})
    _error(response, 409, "windows_key_duplicate")
    assert WindowsProductKey.objects.count() == 1


def test_key_assignments_show_owner_labels_only(api, runner, member):
    key = _add_key(api, seats=2)
    workspace = create(runner, member)
    WindowsWorkspaceLicense.objects.filter(workspace=workspace).update(
        activation_status="activated"
    )
    body = api("get").json()
    assert body["product_keys"][0]["id"] == key["id"]
    assert body["product_keys"][0]["used_seats"] == 1
    assert body["product_keys"][0]["assignments"] == [
        {"owner_label": "Max", "activation_status": "activated"}
    ]
    assert workspace.name not in json.dumps(body)
    assert body["usage"] == {
        "windows_workspaces": 1,
        "running": 1,
        "device_key": 1,
        "user": 0,
        "unlicensed": 0,
    }


def test_key_seats_cannot_drop_below_used(api, runner, member):
    key = _add_key(api, seats=2)
    create(runner, member)
    detail = _error(
        api("patch", f"keys/{key['id']}/", {"seats": 0}), 400, "validation_error"
    )
    assert "seats" in detail
    settle(create(runner, member), WorkspaceStatus.STOPPED)
    _error(
        api("patch", f"keys/{key['id']}/", {"seats": 1}),
        409,
        "windows_key_seats_in_use",
    )
    body = api("patch", f"keys/{key['id']}/", {"seats": 2, "label": "Pool"}).json()
    assert (body["seats"], body["label"], body["used_seats"]) == (2, "Pool", 2)


def test_key_with_assigned_seats_cannot_be_deleted(api, runner, member):
    key = _add_key(api, seats=2)
    create(runner, member)
    create(runner, member)
    detail = _error(api("delete", f"keys/{key['id']}/"), 409, "windows_key_in_use")
    assert "2 workspaces" in detail
    # The database relation protects held seats as well.
    with pytest.raises(RestrictedError):
        WindowsProductKey.objects.get(pk=key["id"]).delete()
    assert WindowsProductKey.objects.filter(pk=key["id"]).exists()


def test_unused_key_is_deleted(api):
    key = _add_key(api)
    assert api("delete", f"keys/{key['id']}/").status_code == 204
    assert not WindowsProductKey.objects.exists()
    _error(api("delete", f"keys/{key['id']}/"), 404, "not_found")


def test_keys_of_other_organizations_are_not_found(api, admin):
    other = Organization.objects.create(name="Other", slug=f"o-{uuid.uuid4().hex}")
    foreign = WindowsProductKey.objects.create(
        organization=other,
        encrypted_key="x",
        key_suffix="DDDDD",
        fingerprint="f",
        seats=1,
    )
    _error(api("patch", f"keys/{foreign.id}/", {"seats": 2}), 404, "not_found")
    _error(api("delete", f"keys/{foreign.id}/"), 404, "not_found")


# -- User licenses -------------------------------------------------------------


def test_grant_and_revoke_user_licenses(api, organization, admin, member):
    api("patch", "", {"user_license_seats": 1})
    body = api("put", f"users/{member.id}/").json()
    assert {m["user_id"]: m["licensed"] for m in body["members"]} == {
        str(admin.id): False,
        str(member.id): True,
    }
    # Granting again is idempotent.
    assert api("put", f"users/{member.id}/").status_code == 200
    _error(api("put", f"users/{admin.id}/"), 409, "windows_user_seats_exhausted")
    _error(
        api("patch", "", {"user_license_seats": 0}), 409, "windows_user_seats_in_use"
    )

    body = api("delete", f"users/{member.id}/").json()
    assert not any(m["licensed"] for m in body["members"])
    assert not WindowsUserLicense.objects.exists()
    assert api("patch", "", {"user_license_seats": 0}).status_code == 200


def test_licenses_are_granted_to_members_only(api):
    api("patch", "", {"user_license_seats": 5})
    outsider = make_user("outsider")
    _error(api("put", f"users/{outsider.id}/"), 404, "not_found")
    assert not WindowsUserLicense.objects.exists()
