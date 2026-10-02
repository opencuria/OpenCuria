"""Regression coverage for credential service visibility across organizations."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.models import Credential, CredentialService
from apps.credentials.services import CredentialOAuthSvc, CredentialSvc
from apps.mcp_app.server import _call_connect_credential_oauth, _call_list_credentials
from apps.organizations.models import Membership, Organization


def _org(label: str) -> Organization:
    """Create a uniquely named test organization."""
    suffix = uuid.uuid4().hex[:8]
    return Organization.objects.create(name=label, slug=f"{label.lower()}-{suffix}")


def _service(organization: Organization | None, label: str) -> CredentialService:
    """Create a simple service definition with the selected visibility."""
    return CredentialService.objects.create(
        organization=organization,
        name=label,
        slug=f"{label.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
        credential_type="mcp_oauth",
        oauth_server_url="https://mcp.example.test/mcp",
    )


def _credential(
    user, service: CredentialService, name: str, *, organization=None
) -> Credential:
    """Create a credential row whose encrypted value is irrelevant to listing."""
    return Credential.objects.create(
        user=None if organization else user,
        organization=organization,
        service=service,
        name=name,
        encrypted_value="opaque-test-value",
        oauth_server_url=service.oauth_server_url,
        oauth_resource=service.oauth_server_url,
        oauth_status="disconnected",
        created_by=user,
    )


@pytest.mark.django_db
def test_credential_listing_filters_personal_credentials_by_service_visibility(
) -> None:
    """Global/current services remain listed and foreign-org services stay hidden."""
    user = get_user_model().objects.create_user(
        email=f"visibility-{uuid.uuid4().hex[:8]}@example.test", password="secret"
    )
    current_org, other_org = _org("Visible"), _org("Foreign")
    global_credential = _credential(user, _service(None, "Global"), "Global account")
    local_credential = _credential(
        user, _service(current_org, "Local"), "Current organization service"
    )
    foreign_credential = _credential(
        user, _service(other_org, "Foreign"), "Foreign organization service"
    )

    listed = CredentialSvc().list_credentials(user, current_org.id)

    assert {row.id for row in listed} == {global_credential.id, local_credential.id}
    assert foreign_credential.id not in {row.id for row in listed}


@pytest.mark.django_db
def test_mcp_credential_listing_uses_same_service_visibility_filter() -> None:
    """MCP metadata listing does not disclose a foreign service name or slug."""
    user = get_user_model().objects.create_user(
        email=f"mcp-visibility-{uuid.uuid4().hex[:8]}@example.test", password="secret"
    )
    current_org, other_org = _org("McpCurrent"), _org("McpForeign")
    Membership.objects.create(user=user, organization=current_org, role="member")
    _credential(user, _service(None, "Visible Global"), "Visible account")
    hidden = _credential(
        user, _service(other_org, "Secret Foreign Service"), "Secret account"
    )
    key = type("CredentialReadKey", (), {"user": user})()

    result = _call_list_credentials(key, current_org.id, {})[0].text

    assert "Visible Global" in result
    assert "Secret Foreign Service" not in result
    assert str(hidden.id) not in result


@pytest.mark.django_db
def test_foreign_service_credentials_cannot_be_updated_deleted_or_disconnected(
) -> None:
    """CRUD and OAuth disconnect fail closed when the service belongs elsewhere."""
    user = get_user_model().objects.create_user(
        email=f"hidden-mutation-{uuid.uuid4().hex[:8]}@example.test", password="secret"
    )
    current_org, other_org = _org("MutationCurrent"), _org("MutationForeign")
    credential = _credential(user, _service(other_org, "Private OAuth"), "Private")
    service = CredentialSvc()

    from common.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        service.update_credential(
            credential_id=credential.id,
            org_id=current_org.id,
            user=user,
            is_admin=False,
            name="stolen metadata",
        )
    with pytest.raises(NotFoundError):
        service.delete_credential(
            credential.id, org_id=current_org.id, user=user, is_admin=False
        )
    with pytest.raises(NotFoundError):
        CredentialOAuthSvc().disconnect(
            credential_id=credential.id, user=user, organization_id=current_org.id
        )

    credential.refresh_from_db()
    assert credential.name == "Private"
    assert Credential.objects.filter(id=credential.id).exists()


@pytest.mark.django_db
def test_mcp_oauth_link_uses_settings_contract_and_scopes_reconnect() -> None:
    """MCP returns only supported UI query links and enforces OAuth ownership."""
    from apps.accounts.models import APIKeyPermission
    from apps.credentials.models import OrgCredentialServiceActivation

    user = get_user_model().objects.create_user(
        email=f"mcp-oauth-link-{uuid.uuid4().hex[:8]}@example.test", password="secret"
    )
    member = get_user_model().objects.create_user(
        email=f"mcp-oauth-member-{uuid.uuid4().hex[:8]}@example.test", password="secret"
    )
    org, foreign_org = _org("OAuthLink"), _org("OAuthOther")
    Membership.objects.create(user=user, organization=org, role="admin")
    Membership.objects.create(user=member, organization=org, role="member")
    service = _service(None, "OAuth Link Service")
    OrgCredentialServiceActivation.objects.create(
        organization=org, credential_service=service
    )
    owned = _credential(user, service, "My account")

    class ApiKey:
        def __init__(self, actor) -> None:
            self.user = actor

        def has_permission(self, permission) -> bool:
            return permission == APIKeyPermission.CREDENTIALS_READ

    from django.test import override_settings

    with override_settings(
        DEBUG=True,
        MCP_OAUTH_FRONTEND_RETURN_URL="http://127.0.0.1:5174/?settings=credentials",
    ):
        add_result = _call_connect_credential_oauth(
            ApiKey(user), org.id, {"service_id": str(service.id)}
        )[0].text
        assert "?settings=credentials&add_credential=" in add_result
        assert str(service.id) in add_result
        assert "credential_action" not in add_result
        assert "name=" not in add_result

        reconnect_result = _call_connect_credential_oauth(
            ApiKey(user), org.id, {"credential_id": str(owned.id)}
        )[0].text
        assert "?settings=credentials&reconnect_credential=" in reconnect_result
        assert str(owned.id) in reconnect_result

        inactive_service = _service(None, "Inactive OAuth")
        inactive_result = _call_connect_credential_oauth(
            ApiKey(user), org.id, {"service_id": str(inactive_service.id)}
        )[0].text
        assert "inactive" in inactive_result
        assert "Admin role required" in _call_connect_credential_oauth(
            ApiKey(member),
            org.id,
            {
                "credential_id": str(
                    _credential(
                        user, service, "Organization account", organization=org
                    ).id
                )
            }
        )[0].text
        foreign_service = _service(foreign_org, "Other Organization OAuth")
        foreign_credential = _credential(user, foreign_service, "Foreign account")
        assert "not found" in _call_connect_credential_oauth(
            ApiKey(user), org.id, {"credential_id": str(foreign_credential.id)}
        )[0].text
