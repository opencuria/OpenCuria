"""API key permission list must expose workspace git permissions."""

from __future__ import annotations

import pytest
from django.test import Client


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
def test_api_key_permissions_include_git_read_and_write(client: Client):
    response = client.get("/api/v1/auth/api-key-permissions/")
    assert response.status_code == 200
    payload = response.json()
    by_value = {entry["value"]: entry for entry in payload}

    for value in ("workspaces:git_read", "workspaces:git_write"):
        assert value in by_value, f"missing permission {value}"
        entry = by_value[value]
        assert entry["group"] == "workspaces"
        assert isinstance(entry["description"], str)
        assert entry["description"].strip() != ""


@pytest.mark.django_db
def test_api_key_permission_enum_values():
    from apps.accounts.models import APIKeyPermission

    assert APIKeyPermission.WORKSPACES_GIT_READ.value == "workspaces:git_read"
    assert APIKeyPermission.WORKSPACES_GIT_WRITE.value == "workspaces:git_write"
