"""API key management requires JWT authentication, never another API key."""

from __future__ import annotations

import pytest
from django.test import Client

from apps.accounts.auth_backends import get_auth_backend
from apps.accounts.models import APIKey, APIKeyPermission, User
from apps.organizations.models import Membership, Organization
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db

API_KEYS_URL = "/api/v1/auth/api-keys/"
ORGANIZATIONS_URL = "/api/v1/organizations/"
READ_ONLY = [APIKeyPermission.WORKSPACES_READ.value]


@pytest.fixture
def user() -> User:
    return User.objects.create_user(email="api-key-owner@example.com")


@pytest.fixture
def restricted_key(user: User) -> tuple[APIKey, str]:
    token = f"kai_{generate_api_token()}"
    key = APIKey.objects.create(
        user=user,
        name="Read-only integration",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=READ_ONLY,
    )
    return key, token


@pytest.fixture(params=["bearer", "x-api-key"])
def api_key_headers(
    request: pytest.FixtureRequest, restricted_key: tuple[APIKey, str]
) -> dict[str, str]:
    _, token = restricted_key
    if request.param == "bearer":
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    return {"HTTP_X_API_KEY": token}


@pytest.fixture
def jwt_client(user: User) -> Client:
    token = get_auth_backend().generate_tokens(user).access_token
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


@pytest.mark.parametrize(
    "permission_fields",
    [{}, {"permissions": []}, {"permissions": APIKeyPermission.all_values()}],
    ids=["omitted", "empty", "explicit-full-access"],
)
def test_restricted_api_key_cannot_issue_full_access_key(
    client: Client,
    restricted_key: tuple[APIKey, str],
    api_key_headers: dict[str, str],
    permission_fields: dict[str, list[str]],
) -> None:
    assert client.get(ORGANIZATIONS_URL, **api_key_headers).status_code == 403
    count = APIKey.objects.count()

    response = client.post(
        API_KEYS_URL,
        data={"name": "Escalated key", **permission_fields},
        content_type="application/json",
        **api_key_headers,
    )

    assert response.status_code == 401
    assert APIKey.objects.count() == count
    assert client.get(ORGANIZATIONS_URL, **api_key_headers).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [{}, {"permissions": []}, {"permissions": APIKeyPermission.all_values()}],
    ids=["omitted", "empty", "explicit-full-access"],
)
def test_restricted_api_key_cannot_escalate_its_own_permissions(
    client: Client,
    restricted_key: tuple[APIKey, str],
    api_key_headers: dict[str, str],
    payload: dict[str, list[str]],
) -> None:
    key, _ = restricted_key
    assert client.get(ORGANIZATIONS_URL, **api_key_headers).status_code == 403

    response = client.patch(
        f"{API_KEYS_URL}{key.id}/",
        data=payload,
        content_type="application/json",
        **api_key_headers,
    )

    assert response.status_code == 401
    key.refresh_from_db()
    assert key.permissions == READ_ONLY
    assert client.get(ORGANIZATIONS_URL, **api_key_headers).status_code == 403


@pytest.mark.parametrize("permissions", [READ_ONLY, APIKeyPermission.all_values()])
@pytest.mark.parametrize("method", ["get", "post", "patch", "delete"])
def test_api_keys_cannot_manage_keys_even_with_full_permissions(
    client: Client,
    restricted_key: tuple[APIKey, str],
    api_key_headers: dict[str, str],
    permissions: list[str],
    method: str,
) -> None:
    key, _ = restricted_key
    key.permissions = permissions
    key.save(update_fields=["permissions"])
    url = f"{API_KEYS_URL}{key.id}/" if method in {"patch", "delete"} else API_KEYS_URL

    response = getattr(client, method)(
        url,
        data={"name": "Unauthorized", "permissions": APIKeyPermission.all_values()},
        content_type="application/json",
        **api_key_headers,
    )

    assert response.status_code == 401
    assert APIKey.objects.count() == 1
    key.refresh_from_db()
    assert key.is_active is True
    assert key.permissions == permissions


@pytest.mark.parametrize("method", ["get", "post", "patch", "delete"])
@pytest.mark.parametrize("session_login", [False, True], ids=["anonymous", "cookie"])
def test_api_key_management_requires_jwt(
    client: Client,
    user: User,
    restricted_key: tuple[APIKey, str],
    method: str,
    session_login: bool,
) -> None:
    key, _ = restricted_key
    if session_login:
        client.force_login(user)
    url = f"{API_KEYS_URL}{key.id}/" if method in {"patch", "delete"} else API_KEYS_URL

    response = getattr(client, method)(
        url,
        data={"name": "Unauthorized", "permissions": APIKeyPermission.all_values()},
        content_type="application/json",
    )

    assert response.status_code == 401
    assert APIKey.objects.count() == 1
    key.refresh_from_db()
    assert key.permissions == READ_ONLY
    assert key.is_active is True


@pytest.mark.parametrize(
    "permission_fields", [{}, {"permissions": []}, {"permissions": READ_ONLY}]
)
def test_jwt_user_can_manage_own_api_keys(
    jwt_client: Client,
    user: User,
    permission_fields: dict[str, list[str]],
) -> None:
    response = jwt_client.post(
        API_KEYS_URL,
        data={"name": "Managed key", **permission_fields},
        content_type="application/json",
    )
    assert response.status_code == 201
    created = response.json()
    key = APIKey.objects.get(id=created["id"])
    assert key.user == user
    assert key.key_hash == hash_token(created["key"])
    assert key.permissions == (
        permission_fields.get("permissions") or APIKeyPermission.all_values()
    )

    response = jwt_client.get(API_KEYS_URL)
    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()] == [str(key.id)]
    assert "key" not in response.json()[0]
    assert "key_hash" not in response.json()[0]

    response = jwt_client.patch(
        f"{API_KEYS_URL}{key.id}/",
        data={"permissions": [APIKeyPermission.ORGANIZATIONS_READ.value]},
        content_type="application/json",
    )
    assert response.status_code == 200
    key.refresh_from_db()
    assert key.permissions == [APIKeyPermission.ORGANIZATIONS_READ.value]
    assert "key" not in response.json()
    assert "key_hash" not in response.json()
    key_client = Client(HTTP_X_API_KEY=created["key"])
    assert key_client.get(ORGANIZATIONS_URL).status_code == 200

    assert jwt_client.delete(f"{API_KEYS_URL}{key.id}/").status_code == 204
    key.refresh_from_db()
    assert key.is_active is False
    assert key_client.get(ORGANIZATIONS_URL).status_code == 401


def test_jwt_user_cannot_manage_another_users_key(
    jwt_client: Client,
    restricted_key: tuple[APIKey, str],
) -> None:
    key, _ = restricted_key
    other_user = User.objects.create_user(email="other-key-owner@example.com")
    key.user = other_user
    key.save(update_fields=["user"])

    assert jwt_client.get(API_KEYS_URL).json() == []
    response = jwt_client.patch(
        f"{API_KEYS_URL}{key.id}/",
        data={"permissions": APIKeyPermission.all_values()},
        content_type="application/json",
    )
    assert response.status_code == 404
    assert jwt_client.delete(f"{API_KEYS_URL}{key.id}/").status_code == 404
    key.refresh_from_db()
    assert key.permissions == READ_ONLY
    assert key.is_active is True


def test_jwt_key_management_rejects_invalid_permissions(
    jwt_client: Client,
    restricted_key: tuple[APIKey, str],
) -> None:
    key, _ = restricted_key
    payload = {"name": "Invalid", "permissions": ["invalid:permission"]}

    response = jwt_client.post(
        API_KEYS_URL, data=payload, content_type="application/json"
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_permissions"
    assert APIKey.objects.count() == 1

    response = jwt_client.patch(
        f"{API_KEYS_URL}{key.id}/", data=payload, content_type="application/json"
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_permissions"
    key.refresh_from_db()
    assert key.permissions == READ_ONLY


def test_restricted_api_key_still_reads_workspaces(
    client: Client,
    user: User,
    api_key_headers: dict[str, str],
) -> None:
    organization = Organization.objects.create(
        name="Test organization", slug="test-org"
    )
    Membership.objects.create(user=user, organization=organization)

    response = client.get(
        "/api/v1/workspaces/",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
        **api_key_headers,
    )

    assert response.status_code == 200
    assert response.json() == []
    assert client.get(ORGANIZATIONS_URL, **api_key_headers).status_code == 403
