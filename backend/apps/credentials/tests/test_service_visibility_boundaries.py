"""Regression coverage for credential service visibility across organizations."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.models import Credential, CredentialService
from apps.credentials.services import CredentialOAuthSvc, CredentialSvc
from apps.organizations.models import Organization


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
