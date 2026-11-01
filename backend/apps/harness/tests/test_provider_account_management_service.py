"""Provider management use cases can run independently of HTTP adapters."""

from __future__ import annotations

import uuid

import pytest

from apps.harness.models import ProviderConnection
from apps.harness.provider_account_management_service import (
    ProviderAccountManagementService,
)
from apps.harness.provider_accounts import ProviderAccountService
from common.exceptions import NotFoundError


@pytest.mark.django_db
def test_account_management_returns_safe_metadata_through_the_lifecycle(
    organization, provider_account_user
) -> None:
    """Non-HTTP callers get normalized metadata, partial updates and exact deletion."""
    service = ProviderAccountManagementService()
    values = {
        "organization_id": organization.id,
        "user": provider_account_user,
        "provider": "openai-compatible",
    }
    row = service.create_account(
        **values,
        payload={
            "label": "Endpoint",
            "base_url": "https://example.test/v1/",
            "models": [" model ", "model"],
            "api_key": "private-credential",
        },
    )
    assert row["base_url"] == "https://example.test/v1"
    assert row["models"] == ["model"]
    assert row["is_active"] is True
    assert row["api_key_hint"] == "••••tial"
    assert "private-credential" not in repr(row)
    account_id = uuid.UUID(row["id"])

    updated = service.update_account(
        **values, account_id=account_id, payload={"label": "Renamed"}
    )
    assert updated["id"] == row["id"]
    assert updated["label"] == "Renamed"
    assert updated["models"] == row["models"]
    assert updated["api_key_hint"] == row["api_key_hint"]
    assert service.list_connected_accounts(
        organization.id, provider_account_user.id
    ) == [updated]

    service.delete_account(
        organization.id, provider_account_user.id, values["provider"], account_id
    )
    assert service.list_connected_accounts(
        organization.id, provider_account_user.id
    ) == []


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_provider_mismatch_rejects_mutation_outside_http(
    organization, provider_account_user, provider_account_factory, operation
) -> None:
    """Using another provider's identity never changes an otherwise visible account."""
    account = provider_account_factory()
    service = ProviderAccountManagementService()
    with pytest.raises(NotFoundError):
        if operation == "update":
            service.update_account(
                organization_id=organization.id,
                user=provider_account_user,
                provider="chatgpt",
                account_id=account.id,
                payload={"label": "Rejected"},
            )
        else:
            service.delete_account(
                organization.id, provider_account_user.id, "chatgpt", account.id
            )
    unchanged = ProviderAccountService().get_account(
        organization.id, provider_account_user.id, account.id
    )
    assert unchanged.label == account.label


@pytest.mark.django_db
def test_management_excludes_accounts_without_a_credential_binding(
    organization, provider_account_user, provider_account_factory
) -> None:
    """Unconnected records remain available internally but aren't listed to users."""
    bound = provider_account_factory()
    ProviderConnection.objects.create(
        organization=organization,
        user=provider_account_user,
        provider="chatgpt",
        label="Not connected",
    )
    accounts = ProviderAccountService()
    assert len(accounts.list_accounts(organization.id, provider_account_user.id)) == 2
    rows = ProviderAccountManagementService(accounts).list_connected_accounts(
        organization.id, provider_account_user.id
    )
    assert [row["id"] for row in rows] == [str(bound.id)]
